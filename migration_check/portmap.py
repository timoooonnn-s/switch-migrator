"""Read a port map: which old port went to which new port (decision 0010).

A CSV whose columns are named like switch-migrator's cabling sheet: 'Old switch',
'Old port', 'New switch', 'New port' (any case and spacing; other columns are ignored).
switch-migrator's `--csv` export of the Cabling sheet is read as it is. The .xlsx is
read by switch-migrator itself, which hands the map over; that reader is not copied here.
"""

from __future__ import annotations

import csv
import io
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


def _delimiter(text: str) -> str:
    """The separator this .csv uses, read off its header line.

    Excel writes the list separator of the machine's locale: a comma in the
    US/UK, a semicolon across most of Europe, a tab for "Unicode text". A
    sheet that came back from a technician's laptop has whichever one that
    machine chose, and assuming a comma turned a semicolon export into a
    single column - reported as "needs the columns 'Old switch', ...", which
    sends the reader looking at the header instead of at the delimiter. Same
    rule as switch-migrator's `cabling_sheet._delimiter`, so both readers
    accept the same files.
    """
    header = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: header.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def read_port_map(path: Path) -> PortMapFile:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
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
