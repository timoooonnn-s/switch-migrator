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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from switch_migrator.config import (
    ConsoleServerSettings,
    Credentials,
    SshSettings,
)
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
# remaining command. Configurable as ssh.max_transport_failures.


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


@dataclass
class CommandRecord:
    """One command as it was actually sent - the raw material for the run
    manifest and for the progress display."""
    command: str
    ok: bool
    attempts: int = 1
    duration_ms: int = 0
    error: str = ""


class BaseRunner:
    setup_warnings: list[str]
    name: str = ""
    # every command this runner sent, in order
    command_log: list[CommandRecord]
    # optional progress hook, called as on_command(device, command, index)
    # BEFORE the command is sent. The runner is the single choke point for all
    # device traffic, so this is the one place that sees every command without
    # threading a callback through every collector.
    on_command: "Callable[[str, str, int], None] | None" = None
    # per-command spelling overrides (config `commands:`); applied here at the
    # choke point so collectors, raw capture, dry run and offline replay all
    # see the SAME remapped spelling. Values are validated read-only at load.
    command_overrides: dict[str, str] = {}

    def _map(self, command: str) -> str:
        return self.command_overrides.get(command, command)

    def run(self, command: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def _log_start(self, command: str) -> float:
        if self.on_command is not None:
            try:
                self.on_command(self.name, command, len(self.command_log) + 1)
            except Exception:  # noqa: BLE001 - a broken progress bar is not fatal
                pass
        return time.monotonic()

    def _log_end(self, command: str, started: float, ok: bool,
                 attempts: int = 1, error: str = "") -> None:
        self.command_log.append(CommandRecord(
            command=command, ok=ok, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error[:200]))


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
            # getattr with defaults: a netmiko layout change that drops either
            # attribute must degrade (defaults match today's values), not crash
            # every ERS connect
            prompt = getattr(self, "prompt_pattern", r"(?m:[>#]\s*$)")
            enter = getattr(self, "RETURN", "\r")
            pattern = (r"(?:sername|ssword|[Cc]trl[-\s]?[Yy]|Press [Ee][Nn][Tt][Ee][Rr]"
                       rf"|Menu|{prompt})")
            self.write_channel(enter)  # wake boxes that wait for a keystroke
            for _ in range(6):
                try:
                    chunk = self.read_until_pattern(pattern=pattern, read_timeout=6.0)
                except Exception:  # noqa: BLE001 - silent so far: nudge with Ctrl-Y
                    self.write_channel(_CTRL_Y)
                    time.sleep(0.3 * delay_factor)
                    self.write_channel(enter)
                    continue
                if re.search(prompt, chunk):
                    return
                if re.search(r"[Cc]trl[-\s]?[Yy]", chunk):
                    self.write_channel(_CTRL_Y)
                    time.sleep(0.3 * delay_factor)
                    self.write_channel(enter)
                elif re.search(r"Press [Ee][Nn][Tt][Ee][Rr]", chunk):
                    self.write_channel(enter)
                elif "Menu" in chunk:
                    self.write_channel(_CTRL_C)
                elif "sername" in chunk:
                    self.write_channel((self.username or "") + enter)
                elif "ssword" in chunk:
                    self.write_channel((self.password or "") + enter)
                else:
                    self.write_channel(enter)
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
                        self.write_channel(getattr(self, "RETURN", "\r"))
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
                 raw_dir: Path | None = None,
                 command_overrides: dict[str, str] | None = None,
                 console: str = "",
                 console_server: ConsoleServerSettings | None = None,
                 raw_skip: tuple[str, ...] = ()):
        self.name = name
        self.host = host
        self.platform = platform
        self.ssh = ssh
        self.raw_dir = raw_dir
        # commands whose OUTPUT must not be written to the raw capture. The
        # migration sheets read the running-config for its tagging and then
        # drop the text; writing it to disk here would keep the RADIUS keys and
        # SNMP users the caller just decided not to keep.
        self.raw_skip = tuple(raw_skip)
        self.command_overrides = dict(command_overrides or {})
        self.console = console
        self.console_server = console_server
        self.setup_warnings: list[str] = []
        self.command_log: list[CommandRecord] = []
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
        details: list[str] = []
        for command in commands:
            try:
                out = self._conn.send_command(command, read_timeout=15)
            except Exception as exc:  # noqa: BLE001 - never fail the whole device here
                # a timeout on one spelling must not skip the remaining
                # spellings: recover the channel and try the next form
                details.append(f"'{command}': {exc.__class__.__name__}: {exc}")
                self._recover_channel()
                continue
            if not looks_like_error(out):
                return  # accepted
            answer = " | ".join(
                line.strip() for line in out.splitlines() if line.strip())[:120]
            details.append(f"device rejected '{command}': {answer}")
        self.setup_warnings.append(
            "; ".join(details)[:300]
            + " - long command outputs may stall on this device")
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
                if self._via_console:
                    return self._connect_via_console(creds)
                if connect_cls is not None:
                    direct = {k: v for k, v in params.items() if k != "device_type"}
                    return connect_cls(**direct)
                return ConnectHandler(**params)
            except NetmikoAuthenticationException as exc:
                # never retry auth failures - avoids account lockouts
                raise self._fail(exc, "authentication failed") from exc
            except ConnectionFailed:
                # already a finished, named failure (console-login gave up)
                raise
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

    @property
    def _via_console(self) -> bool:
        """Route through the terminal server? Needs both halves: the switch's
        console line AND the config's console_server section."""
        return bool(self.console and self.console_server
                    and self.console_server.configured)

    def _connect_via_console(self, creds: Credentials):
        """SSH to the terminal server, land on the switch's serial console,
        drive the switch's own console login, then hand the session to the
        platform's netmiko driver (redispatch).

        The terminal-server login itself is shaped by the config's templates
        (Avocent-style user:70NN usernames, or a TCP port per line) - see
        ConsoleServerSettings. Console-server credentials come from
        SM_CONSOLE_USERNAME/SM_CONSOLE_PASSWORD when set, else the switch
        credentials are reused for both hops.
        """
        import os

        from netmiko import ConnectHandler, redispatch

        cs = self.console_server
        cs_user = os.environ.get("SM_CONSOLE_USERNAME") or creds.username
        cs_pass = os.environ.get("SM_CONSOLE_PASSWORD") or creds.password
        username, port = cs.resolve(cs_user, self.console)
        log.debug("[%s] via console server %s port %s as %s (line %s)",
                  self.name, cs.host, port, username, self.console)
        conn = ConnectHandler(
            device_type=cs.device_type, host=cs.host, port=port,
            username=username, password=cs_pass,
            conn_timeout=self.ssh.conn_timeout,
            banner_timeout=max(15, self.ssh.conn_timeout),
            auth_timeout=max(15, self.ssh.conn_timeout),
            session_log=self._session_log,
            session_log_record_writes=False,
        )
        try:
            self._drive_console_login(conn, creds)
            redispatch(conn, device_type=NETMIKO_DEVICE_TYPE[self.platform])
        except Exception:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001 - best effort on a failed hop
                pass
            raise
        return conn

    def _drive_console_login(self, conn, creds: Credentials) -> None:
        """A serial console shows the switch's own login (or ERS's Ctrl-Y
        gate, or a stale session's prompt) - answer whatever appears until a
        CLI prompt is reached."""
        prompt = r"[>#]\s*$"
        pattern = (r"(?:sername|ogin|ssword|[Cc]trl[-\s]?[Yy]"
                   r"|Press [Ee][Nn][Tt][Ee][Rr]|Menu|[>#]\s*$)")
        conn.write_channel("\r\n")     # wake the line; consoles are silent
        for _ in range(10):
            try:
                chunk = conn.read_until_pattern(pattern=pattern,
                                                read_timeout=8.0)
            except Exception:  # noqa: BLE001 - still silent: nudge again
                conn.write_channel(_CTRL_Y)
                time.sleep(0.3)
                conn.write_channel("\r\n")
                continue
            # a chunk ENDING in a prompt is the goal, whatever banner text
            # (e.g. 'Last login: ...') came along with it - check it first
            if re.search(prompt, chunk):
                return
            if re.search(r"[Cc]trl[-\s]?[Yy]", chunk):
                conn.write_channel(_CTRL_Y)
                time.sleep(0.3)
                conn.write_channel("\r\n")
            elif re.search(r"sername|ogin", chunk):
                conn.write_channel(creds.username + "\n")
            elif "ssword" in chunk:
                conn.write_channel(creds.password + "\n")
            elif "Menu" in chunk:
                conn.write_channel(_CTRL_C)
            else:
                conn.write_channel("\r\n")
        raise ConnectionFailed(
            f"{self.name}: connected to console server "
            f"{self.console_server.host} (line {self.console}) but never "
            f"reached a switch CLI prompt - is the line number right and the "
            f"switch console alive?")

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

    def _recover_channel(self) -> None:
        """A ReadTimeout is typically a stuck --More-- pager: quit it and drain
        the channel so the NEXT command isn't swallowed by it."""
        try:
            self._conn.write_channel("q\n")
            time.sleep(0.5)
            self._conn.clear_buffer()
        except Exception:  # noqa: BLE001 - purely best-effort recovery
            pass

    def run(self, command: str) -> str:
        command = self._map(command)
        if self._dead:
            raise CommandError(
                command, "(skipped: session abandoned after repeated transport failures)")
        started = self._log_start(command)
        attempts = 0
        # Transport failures get a second chance: one dropped read or one stuck
        # pager should not cost a whole column of the report. A command the
        # DEVICE rejected is a different thing entirely - that answer will not
        # change on a re-send, so it is never retried.
        for attempt in range(self.ssh.command_retries + 1):
            attempts = attempt + 1
            log.debug("[%s] %s%s", self.name, command,
                      f" (retry {attempt})" if attempt else "")
            try:
                output = self._conn.send_command(
                    command, read_timeout=self.ssh.read_timeout)
            except Exception as exc:  # netmiko ReadTimeout, socket errors, ...
                self._recover_channel()
                if attempt < self.ssh.command_retries and not self._dead:
                    log.info("[%s] '%s' failed (%s) - retrying",
                             self.name, command, exc.__class__.__name__)
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self._transport_failures += 1
                if self._transport_failures >= self.ssh.max_transport_failures:
                    self._dead = True
                    log.error("[%s] abandoning session after %d consecutive "
                              "transport failures", self.name,
                              self._transport_failures)
                err = CommandError(
                    command, f"(transport {exc.__class__.__name__}) {exc}")
                self._log_end(command, started, False, attempts, str(exc))
                raise err from exc
            break
        self._transport_failures = 0
        if self.raw_dir is not None and command not in self.raw_skip:
            # explicit encoding: the default is locale-dependent, and a
            # UnicodeEncodeError here is not a CommandError - it would escalate
            # to 'collection crashed' for the whole device
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / f"{command_slug(command)}.txt").write_text(
                output, encoding="utf-8")
        if looks_like_error(output):
            self._log_end(command, started, False, attempts, "device rejected")
            raise CommandError(command, output)
        self._log_end(command, started, True, attempts)
        return output

    def close(self) -> None:
        try:
            self._conn.disconnect()
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass


class OfflineRunner(BaseRunner):
    """Replays command output from <raw_root>/<device_name>/<command_slug>.txt."""

    def __init__(self, name: str, raw_root: Path,
                 command_overrides: dict[str, str] | None = None):
        self.name = name
        self.setup_warnings: list[str] = []
        self.command_log: list[CommandRecord] = []
        self.command_overrides = dict(command_overrides or {})
        self.device_dir = raw_root / name
        if not self.device_dir.is_dir():
            raise ConnectionFailed(f"{name}: no raw capture directory at {self.device_dir}")

    def run(self, command: str) -> str:
        # same remap as live: a capture saved under an overridden spelling
        # replays under that same spelling
        command = self._map(command)
        started = self._log_start(command)
        path = self.device_dir / f"{command_slug(command)}.txt"
        if not path.is_file():
            self._log_end(command, started, False, error="no capture file")
            raise CommandError(command, f"(offline) no capture file {path}")
        output = path.read_text(encoding="utf-8", errors="replace")
        if looks_like_error(output):
            self._log_end(command, started, False, error="device rejected")
            raise CommandError(command, output)
        self._log_end(command, started, True)
        return output


class DryRunRunner(BaseRunner):
    """Records what WOULD be sent and connects to nothing.

    Every command comes back empty, which walks the collectors down all of
    their fallback chains - so the recorded list is the full set of commands
    the tool could send to this device, not just the ones a particular release
    happens to accept. That is the honest answer to 'prove this tool is
    read-only', and because it is produced by the real collection code path it
    cannot drift away from what the tool actually does.
    """

    def __init__(self, name: str, platform: Platform,
                 command_overrides: dict[str, str] | None = None):
        self.name = name
        self.platform = platform
        self.setup_warnings: list[str] = []
        self.command_log: list[CommandRecord] = []
        self.command_overrides = dict(command_overrides or {})
        # seed the session-setup commands a live run could send: 'enable' (the
        # privilege mode switch) and EVERY paging-disable spelling, fallbacks
        # included - the list must be the complete honest answer, not the happy
        # path's subset
        self.commands: list[str] = [
            self._map(c) for c in ("enable", *_PAGING_DISABLE[platform])]

    def run(self, command: str) -> str:
        # the remap applies here too, so --dry-run publishes the overridden
        # spellings the live run would really send
        command = self._map(command)
        started = self._log_start(command)
        self.commands.append(command)
        self._log_end(command, started, True)
        return ""


def quick_auth_check(host: str, creds: Credentials, ssh: SshSettings) -> str | None:
    """One fast SSH authentication against one device, before the real run.

    Wrong credentials against a whole inventory cost `workers x read_timeout`
    of waiting (and, worse, a lockout on every box at once). This opens a
    single SSH session, authenticates, and closes - no channel, no command.
    Returns None when the login worked, 'auth' when the device refused the
    credentials, or a short reason string for any other failure (unreachable,
    algorithm mismatch, ...), which is NOT proof the credentials are wrong.
    """
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=creds.username, password=creds.password,
                       timeout=ssh.conn_timeout,
                       banner_timeout=max(15, ssh.conn_timeout),
                       auth_timeout=max(15, ssh.conn_timeout),
                       allow_agent=False, look_for_keys=False)
        return None
    except paramiko.AuthenticationException:
        return "auth"
    except Exception as exc:  # noqa: BLE001 - reachability, algorithms, ...
        first = str(exc).splitlines()[0] if str(exc) else ""
        return f"{exc.__class__.__name__}: {first}"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
