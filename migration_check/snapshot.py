"""Snapshots on disk: raw switch output in switch-migrator's layout (decision 0002).

    <snapshot>/snapshot.json              when, which tool, platforms, failed commands
    <snapshot>/<device>/<command_slug>.txt

A directory without `snapshot.json` is read as a switch-migrator `raw/` folder.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from migration_check import __version__
from migration_check.model import Platform

SNAPSHOT_FILE = "snapshot.json"
SNAPSHOT_FORMAT = 1


def command_slug(command: str) -> str:
    """The file name of a command's output. Must stay identical to switch-migrator's
    `connection.command_slug`, so either tool reads the other's folders."""
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


def _device_dir_name(device: str) -> str:
    return re.sub(r"[\\/]+", "_", device).strip() or "_"


class CommandStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"  # the switch answered with an error message
    FAILED = "failed"  # no answer: timeout, lost session


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command: str
    status: CommandStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RawDevice:
    """One device's folder: its platform and the text of every command saved."""

    name: str
    platform: Platform
    outputs: Mapping[str, str]  # command slug -> text
    records: Mapping[str, CommandRecord] = field(default_factory=dict)  # by command
    host: str | None = None

    def output(self, command: str) -> str | None:
        return self.outputs.get(command_slug(command))

    def why_missing(self, command: str) -> str:
        record = self.records.get(command)
        if record is not None and record.status is not CommandStatus.OK:
            return f"{command!r} {record.status.value}: {record.error or 'no detail'}"
        return f"{command!r} not in the snapshot"


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    devices: tuple[RawDevice, ...]
    taken_at: datetime | None
    label: str | None = None
    tool: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        when = self.taken_at.strftime("%Y-%m-%d %H:%M") if self.taken_at else "time unknown"
        return f"{self.label or self.path.name} ({when})"


class SnapshotError(Exception):
    pass


def read_snapshot(path: Path, default_platform: Platform | None = None) -> Snapshot:
    if not path.is_dir():
        raise SnapshotError(f"{path}: not a directory")
    meta_file = path / SNAPSHOT_FILE
    if meta_file.is_file():
        return _read_own(path, json.loads(meta_file.read_text(encoding="utf-8")))
    return _read_foreign(path, default_platform)


def _load_outputs(device_dir: Path) -> dict[str, str]:
    return {
        file.stem: file.read_text(encoding="utf-8", errors="replace")
        for file in sorted(device_dir.glob("*.txt"))
    }


def _read_own(path: Path, meta: dict[str, object]) -> Snapshot:
    if meta.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError(f"{path}: snapshot format {meta.get('format')!r} is not supported")
    devices_meta = meta.get("devices")
    if not isinstance(devices_meta, dict):
        raise SnapshotError(f"{path}: {SNAPSHOT_FILE} has no devices")
    devices = []
    for name, info in devices_meta.items():
        records = {
            command: CommandRecord(command, CommandStatus(entry["status"]), entry.get("error"))
            for command, entry in info.get("commands", {}).items()
        }
        devices.append(
            RawDevice(
                name=name,
                platform=Platform.from_name(info["platform"]),
                outputs=_load_outputs(path / info.get("folder", _device_dir_name(name))),
                records=records,
                host=info.get("host"),
            )
        )
    taken_at = meta.get("taken_at")
    return Snapshot(
        path=path,
        devices=tuple(devices),
        taken_at=datetime.fromisoformat(taken_at) if isinstance(taken_at, str) else None,
        label=meta.get("label") if isinstance(meta.get("label"), str) else None,  # type: ignore[arg-type]
        tool=meta.get("tool") if isinstance(meta.get("tool"), str) else None,  # type: ignore[arg-type]
    )


# File names that only one platform writes. Fabric Engine is checked first: a BOSS
# folder never holds `show vlan basic`, while both can hold `show vlan` (and `show
# sys-info`, which is why that one is no marker).
_PLATFORM_MARKERS = (
    (Platform.FABRIC_ENGINE, ("show_vlan_mac_address_entry", "show_vlan_basic")),
    (Platform.BOSS, ("show_mac_address_table", "show_vlan")),
)


def guess_platform(outputs: Mapping[str, str]) -> Platform | None:
    for platform, markers in _PLATFORM_MARKERS:
        if any(marker in outputs for marker in markers):
            return platform
    return None


def _read_foreign(path: Path, default_platform: Platform | None) -> Snapshot:
    devices = []
    newest = 0.0
    for device_dir in sorted(p for p in path.iterdir() if p.is_dir()):
        outputs = _load_outputs(device_dir)
        if not outputs:
            continue
        newest = max(newest, *(f.stat().st_mtime for f in device_dir.glob("*.txt")))
        # the files decide; --platform only fills in where they cannot, so one option
        # works for a BOSS "before" and a Fabric Engine "after" folder alike
        platform = guess_platform(outputs) or default_platform
        if platform is None:
            raise SnapshotError(
                f"{device_dir}: cannot tell the platform from its files; pass --platform"
            )
        devices.append(RawDevice(name=device_dir.name, platform=platform, outputs=outputs))
    if not devices:
        raise SnapshotError(f"{path}: no device folders with .txt files")
    return Snapshot(
        path=path,
        devices=tuple(devices),
        taken_at=datetime.fromtimestamp(newest, tz=UTC),
        notes=(f"no {SNAPSHOT_FILE}: read as a switch-migrator raw folder, time from file dates",),
    )


class SnapshotWriter:
    """Saves raw output as it arrives. Safe to call from several capture threads."""

    def __init__(self, path: Path, label: str | None = None) -> None:
        if path.exists() and any(path.iterdir()):
            raise SnapshotError(f"{path}: exists and is not empty; a snapshot is never overwritten")
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.label = label
        self.taken_at = datetime.now(tz=UTC)
        self._devices: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def add_device(self, name: str, platform: Platform, host: str | None) -> None:
        folder = _device_dir_name(name)
        (self.path / folder).mkdir(exist_ok=True)
        with self._lock:
            self._devices[name] = {
                "platform": platform.value,
                "host": host,
                "folder": folder,
                "commands": {},
            }

    def save(
        self,
        device: str,
        command: str,
        text: str | None,
        status: CommandStatus,
        error: str | None = None,
    ) -> None:
        info = self._devices[device]
        if text is not None:
            # rejected output is kept too: it is the evidence of why
            (self.path / str(info["folder"]) / f"{command_slug(command)}.txt").write_text(
                text, encoding="utf-8"
            )
        entry: dict[str, str] = {"status": status.value}
        if error:
            entry["error"] = error
        with self._lock:
            commands = info["commands"]
            assert isinstance(commands, dict)
            commands[command] = entry

    def finish(self) -> Path:
        meta = {
            "format": SNAPSHOT_FORMAT,
            "tool": f"migration-check {__version__}",
            "label": self.label,
            "taken_at": self.taken_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "devices": self._devices,
        }
        meta_file = self.path / SNAPSHOT_FILE
        meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return meta_file
