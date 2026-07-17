"""SSH command execution layer.

Two implementations of the same tiny interface:

* SshRunner       - live netmiko session (extreme_vsp / extreme_ers)
* OfflineRunner   - replays raw command output previously saved with
                    --save-raw; used for offline testing / troubleshooting.

Every runner returns the raw text of a command or raises CommandError.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from pathlib import Path

from switch_migrator.config import Credentials, SshSettings
from switch_migrator.models import Platform

log = logging.getLogger(__name__)

NETMIKO_DEVICE_TYPE = {
    Platform.VOSS: "extreme_vsp",
    Platform.ERS: "extreme_ers",
}

# Error markers across VOSS 8.x-9.4.x and ERS/BOSS
_ERROR_MARKERS = (
    "% invalid input",
    "% incomplete command",
    "% unrecognized command",
    "% cannot modify",
    "error: invalid",
    "invalid command",
    "ambiguous command",
)


class ConnectionFailed(Exception):
    pass


_LEGACY_KEX = (
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
)
_LEGACY_CIPHERS = ("aes256-cbc", "aes192-cbc", "aes128-cbc", "3des-cbc")
_LEGACY_KEYS = ("ssh-rsa", "ssh-dss")

_legacy_enabled = False
_legacy_lock = threading.Lock()

# consecutive transport failures (timeouts, socket errors) after which a
# device session is abandoned instead of burning read_timeout on every
# remaining command
_MAX_TRANSPORT_FAILURES = 2


def enable_legacy_ssh_algorithms() -> list[str]:
    """Append the legacy SSH algorithms old ERS/BOSS gear needs (SHA-1 kex,
    CBC ciphers, ssh-rsa/ssh-dss host keys) to paramiko's client preference
    lists, when this paramiko build implements them.

    Appending to the END keeps modern algorithms first, so connections to
    current devices (VOSS, DvR controllers) are completely unaffected - only
    servers that offer nothing better fall back to these. Purely client-side
    and process-wide; no device or OS configuration is touched. Idempotent.
    Returns the algorithms that were newly enabled.
    """
    global _legacy_enabled
    from paramiko.transport import Transport

    added: list[str] = []
    with _legacy_lock:
        if _legacy_enabled:
            return added

        def extend(attr: str, wanted: tuple[str, ...], implemented) -> None:
            current = list(getattr(Transport, attr))
            for algo in wanted:
                if algo not in current and algo in implemented:
                    current.append(algo)
                    added.append(algo)
            setattr(Transport, attr, tuple(current))

        extend("_preferred_kex", _LEGACY_KEX, Transport._kex_info)
        extend("_preferred_ciphers", _LEGACY_CIPHERS, Transport._cipher_info)
        extend("_preferred_keys", _LEGACY_KEYS, Transport._key_info)
        _legacy_enabled = True
    if added:
        log.info("legacy SSH algorithms enabled: %s", ", ".join(added))
    missing = [a for a in _LEGACY_KEYS + _LEGACY_KEX + _LEGACY_CIPHERS
               if a not in {**Transport._kex_info, **Transport._cipher_info,
                            **Transport._key_info}]
    if missing:
        log.warning("this paramiko build does not implement %s - very old "
                    "ERS gear may still refuse to connect (install "
                    "'paramiko>=3.4,<4')", ", ".join(missing))
    return added


class CommandError(Exception):
    def __init__(self, command: str, output: str):
        super().__init__(f"device rejected command '{command}'")
        self.command = command
        self.output = output


def command_slug(command: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


_ERROR_LINE_KEYWORDS = ("invalid", "incomplete", "unrecognized", "ambiguous",
                        "not allowed", "cannot modify")


def looks_like_error(output: str) -> bool:
    head = output.strip().lower()[:200]
    if any(marker in head for marker in _ERROR_MARKERS):
        return True
    # VOSS can print a 'Command Execution Time' banner (>200 chars) before an
    # error, so also scan every line for %-prefixed CLI error messages
    for line in output.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("%") and any(k in stripped for k in _ERROR_LINE_KEYWORDS):
            return True
    return False


class BaseRunner:
    setup_warnings: list[str]

    def run(self, command: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


# paging left active stalls long outputs at --More-- and the stuck pager
# swallows the next command's characters
_PAGING_DISABLE = {
    Platform.VOSS: "terminal more disable",
    Platform.ERS: "terminal length 0",
}


def _patient_ers_class():
    """netmiko's ExtremeErsSSH with more forgiving prompt detection.

    On these boxes SSH negotiation and the 'Enter Ctrl-Y' login handler
    succeed, but the stock session_preparation then calls find_prompt(), which
    writes a single carriage-return and waits netmiko's default (~10s) for a
    '#'/'>' prompt. Slow ERS/BOSS CPUs, or a login banner that is still
    draining, make that first read miss - raising exactly the

        ReadTimeout: Pattern not detected: '(?:\\#|>)'

    the field is seeing, even though the device is perfectly reachable. Here we
    retry set_base_prompt a few times, draining and re-nudging the channel
    between attempts. ERS-only; the VOSS path is untouched. Returns None if this
    netmiko build can't be subclassed as expected (we then fall back to stock).
    """
    try:
        from netmiko.extreme.extreme_ers_ssh import ExtremeErsSSH
    except Exception:  # noqa: BLE001 - unexpected netmiko layout: use stock class
        return None

    class PatientExtremeErs(ExtremeErsSSH):
        def session_preparation(self) -> None:
            last_exc: Exception | None = None
            for attempt in range(4):
                try:
                    self.set_base_prompt()
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - ReadTimeout/ValueError
                    last_exc = exc
                    try:
                        self.clear_buffer()
                        self.write_channel(self.RETURN)
                        time.sleep(1.0 + attempt)
                        self.clear_buffer()
                    except Exception:  # noqa: BLE001 - best-effort re-nudge
                        pass
            if last_exc is not None:
                raise last_exc
            self.set_terminal_width()
            self.disable_paging()

    return PatientExtremeErs


class SshRunner(BaseRunner):
    def __init__(self, name: str, host: str, platform: Platform,
                 creds: Credentials, ssh: SshSettings,
                 raw_dir: Path | None = None):
        self.name = name
        self.host = host
        self.platform = platform
        self.ssh = ssh
        self.raw_dir = raw_dir
        self.setup_warnings: list[str] = []
        self._transport_failures = 0
        self._dead = False
        # captures the login/banner bytes so a connect failure can show what the
        # device actually sent instead of an opaque 'pattern not detected'
        self._session_log = io.BytesIO()
        if ssh.legacy_algorithms:
            enable_legacy_ssh_algorithms()
        self._conn = self._connect(creds)
        # connected: stop the in-memory session log from growing with every
        # command's output (per-device output can be large on DvR controllers)
        try:
            if getattr(self._conn, "session_log", None) is not None:
                self._conn.session_log.session_log = None
        except Exception:  # noqa: BLE001 - purely a memory optimisation
            pass
        self._ensure_paging_disabled()

    def _captured_tail(self, limit: int = 1000) -> str:
        """Sanitised tail of what the device sent during connect, for errors."""
        try:
            data = self._session_log.getvalue().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
        # collapse to single spaced-out lines so it fits on one report row
        data = " | ".join(ln.strip() for ln in data.splitlines() if ln.strip())
        return data[-limit:].strip()

    def _ensure_paging_disabled(self) -> None:
        """netmiko's session_preparation sends the paging-disable command too,
        but only verifies the command ECHO - not whether the device accepted
        it. If the pager stays active, every long output (Port Interface,
        Port State on 50-port boxes) stalls at --More-- and the stuck pager
        swallows the next command's characters. So send it again explicitly
        and check the device's actual answer.
        """
        command = _PAGING_DISABLE[self.platform]
        try:
            out = self._conn.send_command(command, read_timeout=15)
        except Exception as exc:  # noqa: BLE001 - never fail the whole device here
            self.setup_warnings.append(
                f"could not verify paging disable ('{command}'): "
                f"{exc.__class__.__name__}: {exc} - long command outputs may "
                f"stall on this device")
            log.warning("[%s] %s", self.name, self.setup_warnings[-1])
            return
        if looks_like_error(out):
            detail = " | ".join(
                line.strip() for line in out.splitlines() if line.strip())[:120]
            self.setup_warnings.append(
                f"device rejected '{command}': {detail} - long command "
                f"outputs may stall on this device")
            log.warning("[%s] %s", self.name, self.setup_warnings[-1])

    def _connect(self, creds: Credentials):
        # Imported lazily so parsers/tests work without netmiko installed.
        from netmiko import ConnectHandler
        from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

        params = {
            "device_type": NETMIKO_DEVICE_TYPE[self.platform],
            "host": self.host,
            "username": creds.username,
            "password": creds.password,
            "conn_timeout": self.ssh.conn_timeout,
            "banner_timeout": max(15, self.ssh.conn_timeout),
            "auth_timeout": max(15, self.ssh.conn_timeout),
            "read_timeout_override": self.ssh.read_timeout,
            "fast_cli": False,  # old ERS gear chokes on fast_cli
            "session_log": self._session_log,
            "session_log_record_writes": False,
        }
        if self.ssh.global_delay_factor and self.ssh.global_delay_factor != 1.0:
            params["global_delay_factor"] = self.ssh.global_delay_factor
        if self.ssh.default_enter:
            # some ERS/BOSS boxes only answer a CR+LF ('\r\n'); the netmiko
            # default is '\n' and leaves find_prompt waiting forever
            params["default_enter"] = self.ssh.default_enter
        if self.ssh.disabled_algorithms:
            # last-resort pinning: exclude a modern kex/cipher/host-key a device
            # negotiates but implements badly (garbled channel -> prompt timeout)
            params["disabled_algorithms"] = self.ssh.disabled_algorithms

        # ERS/BOSS: swap in the more patient prompt handling by instantiating a
        # subclass directly (ConnectHandler only dispatches by string name).
        connect_cls = None
        if self.platform is Platform.ERS:
            connect_cls = _patient_ers_class()

        last_exc: Exception | None = None
        for attempt in range(self.ssh.retries + 1):
            try:
                log.debug("connecting to %s (%s) as %s, attempt %d",
                          self.name, self.host, creds.username, attempt + 1)
                if connect_cls is not None:
                    direct = {k: v for k, v in params.items() if k != "device_type"}
                    return connect_cls(**direct)
                return ConnectHandler(**params)
            except NetmikoAuthenticationException as exc:
                # never retry auth failures - avoids account lockouts
                raise ConnectionFailed(
                    f"{self.name}: authentication failed{self._diag_suffix()}") from exc
            except (NetmikoTimeoutException, OSError) as exc:
                last_exc = exc
                if attempt < self.ssh.retries:
                    time.sleep(2 * (attempt + 1))
            except Exception as exc:
                # e.g. paramiko SSHException 'Incompatible ssh peer (no
                # acceptable kex/host key algorithm)' - retrying won't help.
                # This is the ACTUAL cipher/kex case (fails before any channel
                # data); the ReadTimeout below is not.
                raise ConnectionFailed(
                    f"{self.name}: {exc.__class__.__name__}: {exc}"
                    f"{self._diag_suffix()}") from exc
        raise ConnectionFailed(f"{self.name}: {last_exc}{self._diag_suffix()}") from last_exc

    def _diag_suffix(self) -> str:
        """Append what the device sent during connect. When there IS captured
        output, SSH negotiation already succeeded, so the failure is prompt/
        banner handling (NOT ciphers) - say so, because 'pattern not detected'
        is otherwise routinely misread as a cipher problem.
        """
        tail = self._captured_tail()
        if not tail:
            return ""
        return (f" [SSH negotiated OK, so this is not a cipher problem; the "
                f"device sent, but no #/> prompt was matched: '{tail}']")

    def run(self, command: str) -> str:
        if self._dead:
            raise CommandError(
                command, "(skipped: session abandoned after repeated transport failures)")
        log.debug("[%s] %s", self.name, command)
        try:
            output = self._conn.send_command(command, read_timeout=self.ssh.read_timeout)
        except Exception as exc:  # netmiko ReadTimeout, socket errors, ...
            self._transport_failures += 1
            # a ReadTimeout is typically a stuck --More-- pager: quit it and
            # drain the channel so the NEXT command isn't swallowed by it
            try:
                self._conn.write_channel("q\n")
                time.sleep(0.5)
                self._conn.clear_buffer()
            except Exception:  # noqa: BLE001 - purely best-effort recovery
                pass
            if self._transport_failures >= _MAX_TRANSPORT_FAILURES:
                self._dead = True
                log.error("[%s] abandoning session after %d consecutive "
                          "transport failures", self.name, self._transport_failures)
            raise CommandError(
                command, f"(transport {exc.__class__.__name__}) {exc}") from exc
        self._transport_failures = 0
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / f"{command_slug(command)}.txt").write_text(output)
        if looks_like_error(output):
            raise CommandError(command, output)
        return output

    def close(self) -> None:
        try:
            self._conn.disconnect()
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass


class OfflineRunner(BaseRunner):
    """Replays command output from <raw_root>/<device_name>/<command_slug>.txt."""

    def __init__(self, name: str, raw_root: Path):
        self.name = name
        self.setup_warnings: list[str] = []
        self.device_dir = raw_root / name
        if not self.device_dir.is_dir():
            raise ConnectionFailed(f"{name}: no raw capture directory at {self.device_dir}")

    def run(self, command: str) -> str:
        path = self.device_dir / f"{command_slug(command)}.txt"
        if not path.is_file():
            raise CommandError(command, f"(offline) no capture file {path}")
        output = path.read_text()
        if looks_like_error(output):
            raise CommandError(command, output)
        return output
