"""Persist a whole collection run to JSON, and read it back.

Collection is the expensive, disruptive part of this tool: it needs
credentials, network reach and a maintenance-friendly moment, and on a big
inventory it takes minutes. Every report the toolkit produces is a pure
function of the collected state, so that state is worth keeping.

A snapshot is that state - every SwitchAudit plus the FabricState - written as
plain JSON. With one on disk you can re-run any report, try a different I-SID
convention, or hand the file to a colleague, without touching a single switch
again. It is also the record of what the network looked like before the
migration.

The format is deliberately boring: dataclasses in, dataclasses out, with the
version of the tool that wrote it. Unknown fields are ignored on load and
missing ones fall back to the dataclass default, so a snapshot taken by an
older build still opens.

A snapshot contains device data - hostnames, IPs, MAC addresses, LLDP
neighbors and (with --extract-config) the running-config. Treat the file like
the switch output it is.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args, get_origin

from switch_migrator import __version__
from switch_migrator.models import (
    FabricIsid,
    FabricState,
    IstState,
    MltState,
    Platform,
    PortState,
    SwitchAudit,
    VlanInfo,
)

SNAPSHOT_FORMAT = 1


class SnapshotError(Exception):
    pass


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """dataclass/set/enum -> something json.dump understands."""
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Platform):
        return value.value
    if isinstance(value, set):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def to_dict(audits: list[SwitchAudit], fabric: FabricState,
            meta: dict | None = None) -> dict:
    return {
        "format": SNAPSHOT_FORMAT,
        "tool_version": __version__,
        "created": datetime.now().isoformat(timespec="seconds"),
        "meta": meta or {},
        "fabric": _plain(fabric),
        "switches": [_plain(a) for a in audits],
    }


def save(path: Path, audits: list[SwitchAudit], fabric: FabricState,
         meta: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(audits, fabric, meta), indent=2))
    return path


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _build(cls, data: dict):
    """Rebuild a dataclass from a plain dict.

    Fields the snapshot does not carry keep their default (an older file), and
    fields it carries but the dataclass no longer has are dropped (a newer
    file) - so neither direction of version skew is fatal.
    """
    if not isinstance(data, dict):
        raise SnapshotError(f"expected an object for {cls.__name__}, "
                            f"got {type(data).__name__}")
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _coerce(f.type, data[f.name])
    return cls(**kwargs)


_NESTED = {"PortState": PortState, "MltState": MltState, "VlanInfo": VlanInfo,
           "IstState": IstState, "FabricIsid": FabricIsid}


def _coerce(annotation: Any, value: Any) -> Any:
    """Turn a JSON value back into what the field's annotation asks for.

    Annotations arrive as strings here (the modules use
    `from __future__ import annotations`), so this matches on the text - which
    is enough for the handful of shapes the model actually uses.
    """
    text = annotation if isinstance(annotation, str) else str(annotation)
    if value is None:
        return None
    if "Platform" in text:
        return Platform(value)
    for name, cls in _NESTED.items():
        if name not in text:
            continue
        if text.startswith("list[") or get_origin(annotation) is list:
            return [_build(cls, v) for v in value]
        return _build(cls, value)
    if text.startswith("set["):
        return set(value)
    if text.startswith("dict[int"):
        return {int(k): v for k, v in value.items()}
    return value


def _fabric_from(data: dict) -> FabricState:
    fabric = FabricState()
    for key, rec in (data.get("isids") or {}).items():
        isid = _build(FabricIsid, rec)
        isid.names = set(isid.names)
        isid.cvids = {int(c) for c in isid.cvids}
        isid.hosts = set(isid.hosts)
        isid.sources = set(isid.sources)
        isid.seen_on = set(isid.seen_on)
        fabric.isids[int(key)] = isid
    fabric.dvr_errors = list(data.get("dvr_errors") or [])
    fabric.dvrs_ok = list(data.get("dvrs_ok") or [])
    return fabric


def load(path: Path) -> tuple[list[SwitchAudit], FabricState, dict]:
    """Read a snapshot -> (audits, fabric, meta). Raises SnapshotError."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SnapshotError(f"snapshot not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, dict) or "switches" not in data:
        raise SnapshotError(f"{path}: not a switch-migrator snapshot")
    fmt = data.get("format")
    if fmt != SNAPSHOT_FORMAT:
        raise SnapshotError(
            f"{path}: snapshot format {fmt}, this build reads "
            f"{SNAPSHOT_FORMAT} (written by switch-migrator "
            f"{data.get('tool_version', 'unknown')})")
    audits = [_build(SwitchAudit, s) for s in data["switches"]]
    fabric = _fabric_from(data.get("fabric") or {})
    meta = dict(data.get("meta") or {})
    meta.setdefault("created", data.get("created", ""))
    meta.setdefault("tool_version", data.get("tool_version", ""))
    return audits, fabric, meta


def describe(meta: dict, audits: list[SwitchAudit]) -> str:
    """One line for the console when a snapshot is loaded."""
    when = meta.get("created", "unknown time")
    reachable = sum(1 for a in audits if a.reachable)
    return (f"snapshot from {when} (switch-migrator "
            f"{meta.get('tool_version', '?')}): {len(audits)} switch(es), "
            f"{reachable} reachable")
