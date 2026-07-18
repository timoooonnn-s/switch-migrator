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
# swallows the next command's characters. Per platform: primary command first,
# then fallback spellings (the abbreviated VOSS form is field-verified).
_PAGING_DISABLE = {
    Platform.VOSS: ("terminal more disable", "term more dis"),
    Platform.ERS: ("terminal length 0",),
}


_CTRL_Y = "\x19"
_CTRL_C = "\x03"


def _patient_ers_class():
    """netmiko's ExtremeErsSSH with a robust 'Enter Ctrl-Y to begin' login.

    ERS/BOSS gate the CLI behind 'Enter Ctrl-Y to begin', and many boxes stay
    SILENT after SSH auth until they receive a keystroke first. netmiko's stock
    special_login_handler reads BEFORE sending anything, so it times out and the
    session never really enters the OS - the exact symptom from the field. Here
    we proactively drive Ctrl-Y (and the ENTER / username / password / Menu
    gates) until a '#'/'>' prompt appears, then detect the prompt with a couple
    of patient retries for slow CPUs. ERS-only; the VOSS path is untouched.
    Returns None if this netmiko build can't be subclassed (fall back to stock).
    """
    try:
        from netmiko.extreme.extreme_ers_ssh import ExtremeErsSSH
    except Exception:  # noqa: BLE001 - unexpected netmiko layout: use stock class
        return None

    class PatientExtremeErs(ExtremeErsSSH):
        def special_login_handler(self, delay_factor: float = 1.0) -> None:
            prompt = self.prompt_pattern  # r"(?m:[>#]\s*$)"
            pattern = (r"(?:sername|ssword|[Cc]trl-?[Yy]|Press [Ee][Nn][Tt][Ee][Rr]"
                       rf"|Menu|{prompt})")
            self.write_channel(self.RETURN)  # wake boxes that wait for a keystroke
            for _ in range(6):
                try:
                    chunk = self.read_until_pattern(pattern=pattern, read_timeout=6.0)
                except Exception:  # noqa: BLE001 - silent so far: nudge with Ctrl-Y
                    self.write_channel(_CTRL_Y)
                    time.sleep(0.3 * delay_factor)
                    self.write_channel(self.RETURN)
                    continue
                if re.search(prompt, chunk):
                    return
                if re.search(r"[Cc]trl-?[Yy]", chunk):
                    self.write_channel(_CTRL_Y)
                    time.sleep(0.3 * delay_factor)
                    self.write_channel(self.RETURN)
                elif re.search(r"Press [Ee][Nn][Tt][Ee][Rr]", chunk):
                    self.write_channel(self.RETURN)
                elif "Menu" in chunk:
                    self.write_channel(_CTRL_C)
                elif "sername" in chunk:
                    self.write_channel((self.username or "") + self.RETURN)
                elif "ssword" in chunk:
                    self.write_channel((self.password or "") + self.RETURN)
                else:
                    self.write_channel(self.RETURN)
            # Don't hard-fail: session_preparation() retries prompt detection and
            # raises the friendlier connect error if the box truly won't enter.

        def session_preparation(self) -> None:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    self.set_base_prompt()
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - ReadTimeout/ValueError
                    last_exc = exc
                    try:
                        self.clear_buffer()
                        self.write_channel(_CTRL_Y)
                        time.sleep(0.3)
                        self.write_channel(self.RETURN)
                        time.sleep(0.5 + attempt * 0.5)
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
        self._ensure_privileged()
        self._ensure_paging_disabled()

    def _ensure_privileged(self) -> None:
        """Enter privileged EXEC ('#') - VOSS and ERS logins land in user EXEC
        ('>'), where whole command trees the tool needs simply do not exist:
        on VOSS 8.x `show interfaces gigabitEthernet ...` and `show lldp ...`
        are privileged-only, while `show mlt` / `show vlan` / `show virtual-ist`
        work in both modes. That mix was the real cause of the
        '% Invalid input detected' storm on every VOSS access switch - the
        session was never enabled (netmiko's VSP/ERS drivers do not send
        'enable', and an operator types 'ena' interactively without thinking
        about it). 'enable' is a plain mode switch on VOSS/BOSS - no separate
        enable password for the logged-in account.
        """
        try:
            prompt = self._conn.find_prompt().strip()
        except Exception as exc:  # noqa: BLE001 - keep going, commands may still work
            log.warning("[%s] could not read prompt to check privilege level: %s",
                        self.name, exc)
            return
        if prompt.endswith("#"):
            return
        detail = ""
        try:
            self._conn.enable(cmd="enable")
            prompt = self._conn.find_prompt().strip()
        except Exception as exc:  # noqa: BLE001 - e.g. box asks for an enable password
            detail = f" ({exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else exc})"
        if prompt.endswith("#"):
            log.debug("[%s] entered privileged EXEC", self.name)
            return
        self.setup_warnings.append(
            f"could not enter privileged EXEC via 'enable' - prompt stays at "
            f"'>'{detail}; privileged-only commands (show interfaces "
            f"gigabitEthernet, show lldp) will fail on this device - check the "
            f"account's access level")
        log.warning("[%s] %s", self.name, self.setup_warnings[-1])

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
        but (a) only verifies the command ECHO - not whether the device
        accepted it - and (b) runs BEFORE the session is enabled. If the pager
        stays active, every long output stalls at --More-- and the stuck pager
        swallows the next command's characters. So send it again explicitly
        (now in privileged EXEC) and check the device's actual answer; if the
        primary spelling is rejected, fall back to the abbreviated form that
        is field-verified on VOSS ('term more dis').
        """
        commands = _PAGING_DISABLE[self.platform]
        detail = ""
        for command in commands:
            try:
                out = self._conn.send_command(command, read_timeout=15)
            except Exception as exc:  # noqa: BLE001 - never fail the whole device here
                self.setup_warnings.append(
                    f"could not verify paging disable ('{command}'): "
                    f"{exc.__class__.__name__}: {exc} - long command outputs may "
                    f"stall on this device")
                log.warning("[%s] %s", self.name, self.setup_warnings[-1])
                return
            if not looks_like_error(out):
                return  # accepted
            detail = " | ".join(
                line.strip() for line in out.splitlines() if line.strip())[:120]
        self.setup_warnings.append(
            f"device rejected '{commands[0]}'"
            + (f" (and fallback '{commands[-1]}')" if len(commands) > 1 else "")
            + f": {detail} - long command outputs may stall on this device")
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
                raise self._fail(exc, "authentication failed") from exc
            except (NetmikoTimeoutException, OSError) as exc:
                last_exc = exc
                if attempt < self.ssh.retries:
                    time.sleep(2 * (attempt + 1))
            except Exception as exc:
                # e.g. paramiko SSHException 'Incompatible ssh peer (no
                # acceptable kex/host key algorithm)' - retrying won't help.
                # ReadTimeout (prompt not found) also lands here.
                raise self._fail(exc) from exc
        raise self._fail(last_exc) from last_exc

    def _fail(self, exc: Exception, note: str = "") -> "ConnectionFailed":
        """Build a CONCISE, one-line connect failure. The device's login banner
        (which is verbose and noisy for a wall of dead switches) is sent to the
        log file, never into the report. When we captured device bytes the SSH
        transport already succeeded, so it is a prompt/login issue, not ciphers.
        """
        tail = self._captured_tail()
        if tail:
            log.warning("[%s] device output during failed connect (SSH "
                        "negotiated OK; no #/> prompt seen): %s", self.name, tail)
        reason = note or self._clean_reason(exc)
        hint = (" [reached the device but got no CLI prompt - on ERS this is the "
                "'Ctrl-Y to begin' login gate or a slow box; raw output in the "
                "log]") if tail else ""
        return ConnectionFailed(f"{self.name}: {reason}{hint}")

    @staticmethod
    def _clean_reason(exc: Exception) -> str:
        """First non-empty line of the exception - netmiko's ReadTimeout is a
        multi-paragraph blob we do not want spilling into the report."""
        for line in str(exc).splitlines():
            if line.strip():
                return f"{exc.__class__.__name__}: {line.strip()}"
        return exc.__class__.__name__

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
