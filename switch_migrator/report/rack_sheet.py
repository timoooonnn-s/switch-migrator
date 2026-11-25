"""The rack sheet: what the technician holds in the aisle (council decision D6).

A print rendering of the filled-in cabling sheet, one page per OLD switch, in
faceplate order, narrow enough to read on A4: which port, what is on it, where it
goes, a box to tick. Everything an engineer needs to configure the link (VLAN ->
I-SID, tagging, MACs, evidence) stays on the cabling sheet.

Unused ports are not moved, but they are on the page - after the live ones -
because "unused" is the switch's opinion: somebody checks at the rack that no cable
is plugged in after all (round 2, R7).

It is deliberately NOT a second fill-in document. Its columns carry no 'Old port'
header, so the sheet reader never takes it for a cabling sheet: one link is written
down in exactly one place, and that place is the cabling sheet.
"""

from __future__ import annotations

from switch_migrator import usage
from switch_migrator.cabling_sheet import CablingRow, Sheet
from switch_migrator.parsers.common import port_sort_key
from switch_migrator.report.tables import Table

HEADERS = ["Port ID", "Port", "Link", "On it", "Usage", "NEW switch", "NEW port",
           "Rack (new)", "Done", "Note"]

_DEAD = {usage.UNUSED.lower(), usage.LIKELY_UNUSED.lower()}


def _on_it(row: CablingRow) -> str:
    """What the technician looks for: the neighbour as the switch saw it, and
    the port's configured name when it says something else."""
    parts = [row.neighbor] if row.neighbor else []
    if row.port_name and row.port_name.lower() not in row.neighbor.lower():
        parts.append(f'"{row.port_name}"')
    return " ".join(parts)


def _link(row: CablingRow) -> str:
    if row.kind == "uplink":
        return "uplink"
    if row.new_mlt_id is not None:
        return f"MLT {row.new_mlt_id}"
    if row.mlt_id is not None:
        return f"MLT {row.mlt_id} (old)"
    return row.kind or ""


def _note(row: CablingRow) -> str:
    notes = list(row.problems)
    if row.usage.strip().lower() in _DEAD:
        return "unused - not moved; if a cable IS plugged in, tell the planner"
    if not row.migrated:
        notes.append(row.plan_note if row.plan_note.startswith("not planned")
                     else "no new port planned yet")
    if row.usage.strip().upper() == usage.UNCERTAIN:
        notes.append("was down - identify at the rack")
    if not row.neighbor and not row.port_name:
        notes.append("nothing identifies it - read the cable label")
    return "; ".join(notes)


def build_rack_sheets(sheet: Sheet) -> list[Table]:
    """One table per old switch: the ports that move in faceplate order, then the
    ones the usage classification called dead, to be checked."""
    by_switch: dict[str, list[CablingRow]] = {}
    for row in sheet.rows:
        by_switch.setdefault(row.old_switch, []).append(row)

    tables = []
    for switch in sorted(by_switch):
        rows = sorted(by_switch[switch], key=lambda r: (
            r.usage.strip().lower() in _DEAD, port_sort_key(r.old_port)))
        rack = next((r.rack_old for r in rows if r.rack_old), "")
        t = Table(f"Rack {switch}" + (f" ({rack})" if rack else ""), list(HEADERS))
        t.manual_columns = ["Rack (new)", "Done", "Note"]
        t.wrap_columns = ["On it", "Note"]
        t.freeze_columns = 2
        for row in rows:
            t.add([row.uid, row.old_port, _link(row), _on_it(row), row.usage,
                   row.new_switch, row.new_port, row.rack_new, "", _note(row)],
                  "warn" if row.problems else (
                      "warn" if row.usage.strip().lower() in _DEAD else None))
        tables.append(t)
    return tables
