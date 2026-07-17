"""SSH command execution layer.

Two implementations of the same tiny interface:

* SshRunner       - live netmiko session (extreme_vsp / extreme_ers)
* OfflineRunner   - replays raw command output previously saved with
                    --save-raw; used for offline testing / troubleshooting.

Every runner returns the raw text of a command or raises CommandError.
"""

from __future__ import annotations

import logging
import re
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


def looks_like_error(output: str) -> bool:
    head = output.strip().lower()[:200]
    return any(marker in head for marker in _ERROR_MARKERS)


class BaseRunner:
    def run(self, command: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SshRunner(BaseRunner):
    def __init__(self, name: str, host: str, platform: Platform,
                 creds: Credentials, ssh: SshSettings,
                 raw_dir: Path | None = None):
        self.name = name
        self.host = host
        self.platform = platform
        self.ssh = ssh
        self.raw_dir = raw_dir
        if ssh.legacy_algorithms:
            enable_legacy_ssh_algorithms()
        self._conn = self._connect(creds)
        # No session tuning beyond netmiko's own 'terminal more disable':
        # VOSS has no 'terminal width' command, and sending unknown commands
        # desyncs the channel so the NEXT commands read leftover error text.

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
        }
        last_exc: Exception | None = None
        for attempt in range(self.ssh.retries + 1):
            try:
                log.debug("connecting to %s (%s) as %s, attempt %d",
                          self.name, self.host, creds.username, attempt + 1)
                return ConnectHandler(**params)
            except NetmikoAuthenticationException as exc:
                # never retry auth failures - avoids account lockouts
                raise ConnectionFailed(f"{self.name}: authentication failed") from exc
            except (NetmikoTimeoutException, OSError) as exc:
                last_exc = exc
                if attempt < self.ssh.retries:
                    time.sleep(2 * (attempt + 1))
            except Exception as exc:
                # e.g. paramiko SSHException 'Incompatible ssh peer (no
                # acceptable kex/host key algorithm)' - retrying won't help
                raise ConnectionFailed(
                    f"{self.name}: {exc.__class__.__name__}: {exc}") from exc
        raise ConnectionFailed(f"{self.name}: {last_exc}") from last_exc

    def run(self, command: str) -> str:
        log.debug("[%s] %s", self.name, command)
        try:
            output = self._conn.send_command(command, read_timeout=self.ssh.read_timeout)
        except Exception as exc:  # netmiko ReadTimeout, socket errors, ...
            raise CommandError(
                command, f"(transport {exc.__class__.__name__}) {exc}") from exc
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
