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
        self._conn = self._connect(creds)

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
        raise ConnectionFailed(f"{self.name}: {last_exc}") from last_exc

    def run(self, command: str) -> str:
        log.debug("[%s] %s", self.name, command)
        output = self._conn.send_command(command, read_timeout=self.ssh.read_timeout)
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
