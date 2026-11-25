"""Read a port map: which old port went to which new port (decision 0010).

A CSV whose columns are named like switch-migrator's cabling sheet: 'Old switch',
'Old port', 'New switch', 'New port' (any case and spacing; other columns are ignored).
switch-migrator's `--csv` export of the Cabling sheet is read as it is. The .xlsx is
read by switch-migrator itself, which hands the map over; that reader is not copied here.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from migration_check.model import PortKey

_COLUMNS = {"oldswitch": "old_switch", "oldport": "old_port", "newswitch": "new_switch",
            "newport": "new_port"}  # fmt: skip


@dataclass(frozen=True, slots=True)
class PortMapFile:
    mapping: dict[PortKey, PortKey]
    pending: int  # rows with no new switch and port yet: not migrated, not a problem
    problems: tuple[str, ...]


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def normalize_port(value: str) -> str:
    """'Port 1/7', ' 1 / 7 ', '1/7' -> '1/7'; '48' stays '48'. Same rule as
    switch-migrator's `cabling_sheet.normalise_port`, so both read a sheet alike."""
    text = re.sub(r"(?i)^\s*(port|interface|gi|gigabitethernet)\b", "", value).strip()
    return re.sub(r"\s*/\s*", "/", text).strip().strip(",;")


def read_port_map(path: Path) -> PortMapFile:
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = [row for row in csv.reader(file) if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"{path}: empty")
    columns = {_COLUMNS[_key(h)]: i for i, h in enumerate(rows[0]) if _key(h) in _COLUMNS}
    if len(columns) < len(_COLUMNS):
        raise ValueError(
            f"{path}: needs the columns 'Old switch', 'Old port', 'New switch', 'New port'"
        )

    mapping: dict[PortKey, PortKey] = {}
    pending = 0
    problems: list[str] = []
    for number, row in enumerate(rows[1:], start=2):
        cell = {name: (row[i].strip() if i < len(row) else "") for name, i in columns.items()}
        old = (cell["old_switch"], normalize_port(cell["old_port"]))
        new = (cell["new_switch"], normalize_port(cell["new_port"]))
        if not all(old):
            continue  # a spacer or a note line
        if not any(new):
            pending += 1
        elif not all(new):
            problems.append(f"{path.name} row {number}: new switch or new port is empty")
        elif mapping.setdefault(old, new) != new:
            problems.append(
                f"{path.name} row {number}: {old[0]} {old[1]} is mapped twice "
                f"({mapping[old][0]} {mapping[old][1]} and {new[0]} {new[1]})"
            )
    return PortMapFile(mapping, pending, tuple(problems))
