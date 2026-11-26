"""Read a filled-in DC cabling sheet back in.

The tool writes the cabling sheet with empty NEW switch / NEW port / NEW MLT
columns; the planner and the technicians fill those in as they re-patch. This
reads that same file back so the tool can generate the new MLT blocks and, once
the window is over, verify every moved link against what the sheet says should
have happened.

It is deliberately forgiving, because a sheet that has been through a data
centre at 3am is not pristine:

* columns are matched by NAME, not position, so inserting a column of their own
  ("done?", initials, rack) breaks nothing and reordering is fine;
* both the .xlsx and the .csv export are accepted;
* a row whose NEW switch and NEW port are still empty is 'not migrated yet',
  not an error - that is the normal state of most of the sheet mid-window;
* port ids are normalised ("1/7 ", "Port 1/7", "1 / 7" all mean 1/7);
* anything it cannot make sense of is reported as a problem on that row rather
  than raising, so one bad cell never costs the whole sheet.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from switch_migrator.report.excel import SHEET_SCHEMA, STAMP_SHEET

# a stamp row: key | value
KEY_AND_VALUE = 2


class SheetError(Exception):
    pass


@dataclass
class CablingRow:
    """One physical link as the sheet describes it."""
    uid: str = ""                    # migration port ID (P0001)
    old_switch: str = ""
    old_port: str = ""
    new_switch: str = ""             # filled in by the planner/technician
    new_port: str = ""
    new_mlt_id: int | None = None
    new_mlt_name: str = ""
    new_vlan: int | None = None
    # written by hand: no switch knows which rack it lives in
    rack_old: str = ""
    rack_new: str = ""
    # what the port carried before the move - the expectations to verify
    neighbor: str = ""
    port_name: str = ""              # the old port's configured name
    macs: list[str] = field(default_factory=list)
    port_vlans: list[int] = field(default_factory=list)
    port_isids: list[int] = field(default_factory=list)
    mlt_id: int | None = None
    mlt_name: str = ""
    mlt_vlans: list[int] = field(default_factory=list)
    mlt_isids: list[int] = field(default_factory=list)
    # did the OLD aggregation run LACP? None = the sheet does not say
    lacp: bool | None = None
    # ticked by the technician once the cable is moved; None = left blank
    done: bool | None = None
    plan_note: str = ""              # why the plan put it there, or did not
    media: str = ""
    usage: str = ""
    usage_why: str = ""
    kind: str = ""                   # access / mlt / uplink
    tagging: str = ""                # tagged / untagged / mixed, as written
    sheet_vlan: str = ""             # the sheet's leading VLAN column
    row_number: int = 0              # 1-based row in the file, for messages
    sheet: str = ""                  # which worksheet the row came from
    problems: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> bool:
        """Has a technician recorded where this link now lives?"""
        return bool(self.new_switch and self.new_port)

    @property
    def expected_vlans(self) -> list[int]:
        """What the new port should carry: the planner's NEW VLAN when they
        set one, otherwise what the old port carried."""
        if self.new_vlan is not None:
            return [self.new_vlan]
        return self.port_vlans or self.mlt_vlans


@dataclass
class Sheet:
    rows: list[CablingRow] = field(default_factory=list)
    source: Path | None = None
    sheets_read: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # what the hidden stamp sheet said about the build that wrote this file
    written_by: str = ""
    schema: int | None = None

    @property
    def migrated(self) -> list[CablingRow]:
        return [r for r in self.rows if r.migrated]

    @property
    def pending(self) -> list[CablingRow]:
        return [r for r in self.rows if not r.migrated]

    @property
    def tracks_done(self) -> bool:
        """Has anybody ticked the Done column? Then NEW switch/port is the PLAN,
        and only ticked rows have actually moved. Nobody ticked anything: the
        sheet is read as before, NEW switch/port meaning moved."""
        return any(r.done for r in self.rows)

    @property
    def moved(self) -> list[CablingRow]:
        """Rows whose cable has moved - what a verification checks."""
        if not self.tracks_done:
            return self.migrated
        return [r for r in self.migrated if r.done]

    def by_new_switch(self) -> dict[str, list[CablingRow]]:
        out: dict[str, list[CablingRow]] = {}
        for row in self.migrated:
            out.setdefault(row.new_switch, []).append(row)
        return out


# Header text -> field. Matched case-insensitively after stripping everything
# that is not a letter or digit, so 'NEW MLT ID', 'new_mlt_id' and 'New MLT-Id'
# are the same column.
_COLUMNS = {
    "portid": "uid",
    "oldswitch": "old_switch",
    "oldport": "old_port",
    "newswitch": "new_switch",
    "newport": "new_port",
    "newmltid": "new_mlt_id",
    "newmltname": "new_mlt_name",
    "newvlan": "new_vlan",
    "rackold": "rack_old",
    "racknew": "rack_new",
    "tagging": "tagging",
    "enddeviceneighbor": "neighbor",
    "portname": "port_name",
    "macaddresses": "macs",
    "portvlans": "port_vlans",
    "portisids": "port_isids",
    "mltid": "mlt_id",
    "mltname": "mlt_name",
    "mltvlans": "mlt_vlans",
    "mltisids": "mlt_isids",
    "lacp": "lacp",
    "done": "done",
    "plannote": "plan_note",
    "media": "media",
    "usage": "usage",
    "why": "usage_why",
    "type": "kind",
    "vlan": "sheet_vlan",
    "vlanuntagged": "sheet_vlan",
    "untaggedvlan": "sheet_vlan",
}
# Columns the tool WRITES but does not read back: they exist for the human at
# the rack, and their content is already carried by the flat VLAN/I-SID columns
# beside them. Listing them here keeps the sheet from reporting its own
# headers as "columns not written by this tool".
_WRITE_ONLY = {"vlanisid", "mltvlanisid", "vlansource"}

_INT_FIELDS = {"new_mlt_id", "new_vlan", "mlt_id"}
_LIST_INT_FIELDS = {"port_vlans", "port_isids", "mlt_vlans", "mlt_isids"}
_LIST_STR_FIELDS = {"macs"}
_BOOL_FIELDS = {"lacp", "done"}
_TRUE = {"yes", "y", "true", "enable", "enabled", "on", "1", "x", "ok", "done",
         "\u2713", "\u2714", "ja", "j"}
_FALSE = {"no", "n", "false", "disable", "disabled", "off", "0"}

_REQUIRED = ("old_switch", "old_port")
# how the required columns are spelled on the sheet, for the error message
_HEADER_NAMES = {"old_switch": "'Old switch'", "old_port": "'Old port'"}

_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")


def _key(header: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


def normalise_port(value: str) -> str:
    """'Port 1/7', ' 1 / 7 ', '1/7' -> '1/7'; '48' stays '48' (ERS)."""
    text = re.sub(r"(?i)^\s*(port|interface|gi|gigabitethernet)\b", "",
                  str(value or "")).strip()
    text = re.sub(r"\s*/\s*", "/", text)
    return text.strip().strip(",;")


def _ints(value: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", str(value or ""))]


def _one_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _macs(value: str) -> list[str]:
    """MAC cells are written as 'aa:..,bb:..' and may carry a '(+N more)' tail;
    only real MAC-shaped tokens are taken, so the tail cannot become one."""
    return [m.lower() for m in _MAC_RE.findall(str(value or ""))]


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _set_int(row: CablingRow, field_name: str, text: str) -> None:
    if not text:
        return
    parsed = _one_int(text)
    if parsed is None:
        row.problems.append(f"{field_name.replace('_', ' ')} '{text}' is not a number")
    else:
        setattr(row, field_name, parsed)


def _set_bool(row: CablingRow, field_name: str, text: str) -> None:
    word = text.lower()
    if word in _TRUE:
        setattr(row, field_name, True)
    elif word in _FALSE:
        setattr(row, field_name, False)
    elif word not in ("", "?", "-"):
        row.problems.append(f"{field_name.upper()} '{text}' is neither yes nor no")


def _set_field(row: CablingRow, field_name: str, text: str) -> None:
    if field_name in _INT_FIELDS:
        _set_int(row, field_name, text)
    elif field_name in _LIST_INT_FIELDS:
        setattr(row, field_name, _ints(text))
    elif field_name in _LIST_STR_FIELDS:
        setattr(row, field_name, _macs(text))
    elif field_name in _BOOL_FIELDS:
        _set_bool(row, field_name, text)
    elif field_name in ("old_port", "new_port"):
        setattr(row, field_name, normalise_port(text))
    else:
        setattr(row, field_name, text)


def _build_row(mapping: dict[str, object], number: int) -> CablingRow:
    row = CablingRow(row_number=number)
    for field_name, raw in mapping.items():
        _set_field(row, field_name, _cell(raw))
    # a half-filled row is the one thing worth complaining about: it means a
    # technician started the line and something interrupted them
    if row.new_switch and not row.new_port:
        row.problems.append("NEW switch is filled in but NEW port is empty")
    if row.new_port and not row.new_switch:
        row.problems.append("NEW port is filled in but NEW switch is empty")
    return row


def _delimiter(text: str) -> str:
    """The separator a .csv export used, read off its header line.

    Excel writes the list separator of the machine's locale: a comma in the
    US/UK, a semicolon across most of Europe, and a tab for "Unicode text".
    A sheet that came back from a technician's laptop is read with whichever
    one it has - guessing comma and finding one column used to be reported as
    a header row "missing the 'Old switch', 'Old port' column(s)", which sent
    the reader looking in entirely the wrong place.
    """
    header = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: header.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _rows_from_csv(path: Path) -> list[tuple[str, list[list[object]]]]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""),
                        delimiter=_delimiter(text))
    rows: list[list[object]] = [[*row] for row in reader]
    return [(path.name, rows)]


def _looks_like_cabling(rows: list[list[object]]) -> bool:
    # the header is the first NON-EMPTY row - _parse_table drops blank rows
    # too, so a sheet with a leading blank row must not silently vanish here
    for row in rows:
        if not any(_cell(c) for c in row):
            continue
        return any(_key(c) == "oldport" for c in row)
    return False


def _read_stamp(wb: Any) -> dict[str, str]:   # an openpyxl Workbook
    """The hidden stamp sheet the writer leaves behind, as a plain dict.

    Absent on a .csv export and on any workbook an older build wrote, which is
    not an error - it just means the read-back cannot tell whether the columns
    are the ones it expects.
    """
    if STAMP_SHEET not in wb.sheetnames:
        return {}
    stamp: dict[str, str] = {}
    for row in wb[STAMP_SHEET].iter_rows(values_only=True):
        if row and len(row) >= KEY_AND_VALUE and row[0]:
            stamp[str(row[0])] = _cell(row[1])
    return stamp


def _rows_from_xlsx(path: Path,
                    sheet_name: str | None) -> tuple[list[tuple[str, list[list[object]]]],
                                                     dict[str, str]]:
    """Every worksheet that looks like a cabling sheet, in workbook order.

    ALL of them, not just the first: --split-by-location writes one worksheet
    per site ('Cabling Frankfurt', 'Cabling Munich'), and reading only the
    first would verify one site and silently ignore the rest - a migration
    would look complete while half of it was never checked.
    """
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        stamp = _read_stamp(wb)
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                raise SheetError(f"{path}: no sheet named '{sheet_name}' "
                                 f"(found: {', '.join(wb.sheetnames)})")
            names = [sheet_name]
        else:
            names = list(wb.sheetnames)
        # A workbook this tool wrote (it carries the stamp) also holds tables
        # that are not fill-in sheets but have an 'Old port' column (Not moved,
        # Verification). Reading those as cabling rows would count a link twice,
        # so in such a workbook only the Cabling tabs count - unless a human
        # renamed them all, in which case the header decides as for any file.
        if stamp and not sheet_name:
            cabling_tabs = [n for n in names if n.lower().startswith("cabling")]
            names = cabling_tabs or names
        out = []
        for name in names:
            if name == STAMP_SHEET:
                continue
            rows = [list(row) for row in wb[name].iter_rows(values_only=True)]
            if sheet_name or _looks_like_cabling(rows):
                out.append((name, rows))
        if not out:
            raise SheetError(
                f"{path}: no worksheet has an 'Old port' column - is this a "
                f"cabling sheet? (sheets: {', '.join(wb.sheetnames)})")
        return out, stamp
    finally:
        wb.close()


def _parse_table(where: str, raw: list[list[object]], sheet: Sheet, path: Path) -> None:
    raw = [row for row in raw if any(_cell(c) for c in row)]
    if not raw:
        return
    headers = [_key(c) for c in raw[0]]
    index: dict[int, str] = {i: _COLUMNS[h] for i, h in enumerate(headers)
                             if h in _COLUMNS}
    missing = [name for name in _REQUIRED if name not in index.values()]
    if missing:
        pretty = ", ".join(_HEADER_NAMES[n] for n in missing)
        raise SheetError(
            f"{path} ({where}): the header row is missing the {pretty} "
            f"column(s). Use the sheet the tool wrote (--migration-sheets); "
            f"columns may be reordered and extra ones added, but the original "
            f"headers have to survive.")

    for number, values in enumerate(raw[1:], start=2):
        mapping = {index[i]: values[i] for i in index if i < len(values)}
        row = _build_row(mapping, number)
        if not row.old_switch and not row.old_port:
            continue                    # a spacer or a stray note line
        row.sheet = where
        sheet.rows.append(row)

    unknown = [h for h in headers
               if h and h not in _COLUMNS and h not in _WRITE_ONLY]
    if unknown:
        # not a problem - people add their own columns - but worth saying once
        sheet.problems.append(
            f"{where}: {len(unknown)} column(s) not written by this tool were "
            f"kept and ignored")


def _apply_stamp(sheet: Sheet, stamp: dict[str, str]) -> None:
    """Record which build wrote the sheet, and say so if it was not this one.

    Column matching is by name and tolerant, so version skew is rarely fatal -
    but a sheet written by a build whose columns have since changed is exactly
    the case where 'tolerant' quietly means 'reading the wrong thing', and that
    is worth one line in the problems list.
    """
    if not stamp:
        return
    sheet.written_by = stamp.get("tool_version", "")
    raw = stamp.get("schema", "")
    sheet.schema = int(raw) if str(raw).isdigit() else None
    if sheet.schema is not None and sheet.schema != SHEET_SCHEMA:
        sheet.problems.append(
            f"this sheet was written by switch-migrator "
            f"{sheet.written_by or 'an older build'} (sheet schema "
            f"{sheet.schema}, this build writes {SHEET_SCHEMA}) - columns are "
            f"matched by name, so check anything that looks wrong")


def load(path: Path, sheet_name: str | None = None) -> Sheet:
    """Read a filled-in cabling sheet (.xlsx or .csv).

    In an .xlsx, every worksheet that carries an 'Old port' column is read, so
    a workbook split per location comes back whole.
    """
    if not path.is_file():
        raise SheetError(f"cabling sheet not found: {path}")
    suffix = path.suffix.lower()
    stamp: dict[str, str] = {}
    if suffix in (".xlsx", ".xlsm"):
        tables, stamp = _rows_from_xlsx(path, sheet_name)
    elif suffix in (".csv", ".txt", ""):
        tables = _rows_from_csv(path)
    else:
        raise SheetError(f"{path}: unsupported file type '{suffix}' - export "
                         f"the sheet as .xlsx or .csv")

    sheet = Sheet(source=path)
    _apply_stamp(sheet, stamp)
    for where, raw in tables:
        _parse_table(where, raw, sheet, path)
        sheet.sheets_read.append(where)
    if not sheet.rows:
        raise SheetError(f"{path}: no data rows found below the header")
    return sheet
