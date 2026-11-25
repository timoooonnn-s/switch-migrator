"""Take a snapshot. The only module that imports netmiko (decision 0008).

Read-only (R7): it sends nothing but the `show` commands of each platform's adapter.
How a session is opened is pluggable (decision 0009): netmiko here, switch-migrator's
own runners when it calls this tool.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from migration_check.model import Platform
from migration_check.platforms import ADAPTERS
from migration_check.snapshot import CommandStatus, SnapshotWriter

READ_TIMEOUT_SECONDS = 180  # a full MAC table on a busy stack takes a while
_REJECTION_PREFIXES = ("%", "Error:")


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    host: str
    platform: Platform


def read_inventory(path: Path) -> list[Target]:
    """switch-migrator's `switches.yaml` (`switches: [{name, host, platform}]`), or a CSV
    with the columns name, host, platform."""
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # only here: CSV inventories need no PyYAML (decision 0008)

        rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("switches", [])
    else:
        with path.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
    targets = []
    for row in rows:
        if not row.get("platform"):
            raise ValueError(f"{path}: {row.get('name')!r} has no platform")
        name = str(row.get("name") or row.get("host"))
        targets.append(
            Target(name, str(row.get("host") or name), Platform.from_name(row["platform"]))
        )
    return targets


def parse_target(spec: str, platform: Platform) -> Target:
    """'10.1.1.10' or '10.1.1.10=old-access-01'."""
    host, _, name = spec.partition("=")
    return Target(name.strip() or host.strip(), host.strip(), platform)


def looks_rejected(text: str) -> str | None:
    """The switch's error line, if it answered with one instead of output."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][:5]:
        if line.startswith(_REJECTION_PREFIXES):
            return line
    return None


class Session(Protocol):
    """An open, privileged CLI session to one switch (decision 0009).

    `run` returns the switch's text, an error answer included; it raises only when no
    answer came back (timeout, lost session).
    """

    def run(self, command: str) -> str: ...

    def close(self) -> None: ...


Connect = Callable[[Target], Session]


def capture(
    targets: Sequence[Target],
    out: Path,
    connect: Connect,
    *,
    label: str | None = None,
    workers: int = 4,
    log: Callable[[str], None] = print,
) -> Path:
    """Run every target's platform commands through `connect` and save a snapshot."""
    writer = SnapshotWriter(out, label)
    for target in targets:
        writer.add_device(target.name, target.platform, target.host)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(lambda target: _capture_device(connect, target, writer, log), targets))
    return writer.finish()


def _capture_device(
    connect: Connect, target: Target, writer: SnapshotWriter, log: Callable[[str], None]
) -> None:
    commands = ADAPTERS[target.platform].commands
    # Broad excepts on purpose: one unreachable switch or lost session must not cost the
    # other switches their snapshot. Every failure is written down as a command status.
    try:
        session = connect(target)
    except Exception as error:
        log(f"{target.name}: cannot connect: {error}")
        for command in commands:
            writer.save(target.name, command, None, CommandStatus.FAILED, f"connect: {error}")
        return
    try:
        for command in commands:
            try:
                text = session.run(command)
            except Exception as error:
                log(f"{target.name}: {command!r} failed: {error}")
                writer.save(target.name, command, None, CommandStatus.FAILED, str(error))
                continue
            rejection = looks_rejected(text)
            status = CommandStatus.REJECTED if rejection else CommandStatus.OK
            writer.save(target.name, command, text, status, rejection)
    finally:
        session.close()
    log(f"{target.name}: done")


class _NetmikoSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def run(self, command: str) -> str:
        return str(self._connection.send_command(command, read_timeout=READ_TIMEOUT_SECONDS))

    def close(self) -> None:
        self._connection.disconnect()


def netmiko_sessions(username: str, password: str, log: Callable[[str], None] = print) -> Connect:
    """Plain netmiko. Very old BOSS gear may need the legacy SSH algorithms
    switch-migrator enables; it plugs in its own sessions (decision 0009)."""
    try:
        from netmiko import ConnectHandler  # only a live capture needs an SSH stack
    except ImportError:
        raise ValueError("snap and watch need netmiko: pip install -e '.[capture]'") from None

    def connect(target: Target) -> Session:
        connection = ConnectHandler(
            device_type=ADAPTERS[target.platform].netmiko_device_type,
            host=target.host,
            username=username,
            password=password,
        )
        try:
            connection.enable()
        except Exception as error:
            log(f"{target.name}: enable failed ({error}); continuing, commands may be rejected")
        return _NetmikoSession(connection)

    return connect
