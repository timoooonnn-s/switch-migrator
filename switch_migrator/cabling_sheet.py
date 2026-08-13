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
import re
from dataclasses import dataclass, field
from pathlib import Path


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
    # what the port carried before the move - the expectations to verify
    neighbor: str = ""
    macs: list[str] = field(default_factory=list)
    port_vlans: list[int] = field(default_factory=list)
    port_isids: list[int] = field(default_factory=list)
    mlt_id: int | None = None
    mlt_name: str = ""
    mlt_vlans: list[int] = field(default_factory=list)
    mlt_isids: list[int] = field(default_factory=list)
    media: str = ""
    usage: str = ""
    usage_why: str = ""
    kind: str = ""                   # access / mlt / uplink
    sheet_vlan: str = ""             # the sheet's leading VLAN column
    row_number: int = 0              # 1-based row in the file, for messages
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
    problems: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> list[CablingRow]:
        return [r for r in self.rows if r.migrated]

    @property
    def pending(self) -> list[CablingRow]:
        return [r for r in self.rows if not r.migrated]

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
    "enddeviceneighbor": "neighbor",
    "macaddresses": "macs",
    "portvlans": "port_vlans",
    "portisids": "port_isids",
    "mltid": "mlt_id",
    "mltname": "mlt_name",
    "mltvlans": "mlt_vlans",
    "mltisids": "mlt_isids",
    "media": "media",
    "usage": "usage",
    "why": "usage_why",
    "type": "kind",
    "vlan": "sheet_vlan",
}
_INT_FIELDS = {"new_mlt_id", "new_vlan", "mlt_id"}
_LIST_INT_FIELDS = {"port_vlans", "port_isids", "mlt_vlans", "mlt_isids"}
_LIST_STR_FIELDS = {"macs"}

_REQUIRED = ("old_switch", "old_port")
# how the required columns are spelled on the sheet, for the error message
_HEADER_NAMES = {"old_switch": "'Old switch'", "old_port": "'Old port'"}

_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")


def _key(header: str) -> str:
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


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_row(mapping: dict[str, str], number: int) -> CablingRow:
    row = CablingRow(row_number=number)
    for field_name, raw in mapping.items():
        text = _cell(raw)
        if field_name in _INT_FIELDS:
            if text:
                parsed = _one_int(text)
                if parsed is None:
                    row.problems.append(
                        f"{field_name.replace('_', ' ')} '{text}' is not a number")
                else:
                    setattr(row, field_name, parsed)
        elif field_name in _LIST_INT_FIELDS:
            setattr(row, field_name, _ints(text))
        elif field_name in _LIST_STR_FIELDS:
            setattr(row, field_name, _macs(text))
        elif field_name in ("old_port", "new_port"):
            setattr(row, field_name, normalise_port(text))
        else:
            setattr(row, field_name, text)
    # a half-filled row is the one thing worth complaining about: it means a
    # technician started the line and something interrupted them
    if row.new_switch and not row.new_port:
        row.problems.append("NEW switch is filled in but NEW port is empty")
    if row.new_port and not row.new_switch:
        row.problems.append("NEW port is filled in but NEW switch is empty")
    return row


def _rows_from_csv(path: Path) -> list[list[str]]:
    with path.open(newline="") as fh:
        return [row for row in csv.reader(fh)]


def _rows_from_xlsx(path: Path, sheet_name: str | None) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - openpyxl is a hard dependency
        raise SheetError("openpyxl is required to read .xlsx sheets") from None
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                raise SheetError(f"{path}: no sheet named '{sheet_name}' "
                                 f"(found: {', '.join(wb.sheetnames)})")
            ws = wb[sheet_name]
        else:
            # the workbook holds several sheets; take the cabling one, and
            # fall back to the first sheet that has an 'Old port' column
            named = next((n for n in wb.sheetnames if _key(n) == "cabling"), None)
            ws = wb[named] if named else _first_cabling_sheet(wb, path)
        return [[c for c in row] for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _first_cabling_sheet(wb, path: Path):
    for name in wb.sheetnames:
        ws = wb[name]
        for row in ws.iter_rows(max_row=1, values_only=True):
            if any(_key(c) == "oldport" for c in row):
                return ws
    raise SheetError(f"{path}: no sheet with an 'Old port' column - is this a "
                     f"cabling sheet? (sheets: {', '.join(wb.sheetnames)})")


def load(path: Path, sheet_name: str | None = None) -> Sheet:
    """Read a filled-in cabling sheet (.xlsx or .csv)."""
    if not path.is_file():
        raise SheetError(f"cabling sheet not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        raw = _rows_from_xlsx(path, sheet_name)
    elif suffix in (".csv", ".txt", ""):
        raw = _rows_from_csv(path)
    else:
        raise SheetError(f"{path}: unsupported file type '{suffix}' - export "
                         f"the sheet as .xlsx or .csv")

    raw = [row for row in raw if any(_cell(c) for c in row)]
    if not raw:
        raise SheetError(f"{path}: the sheet is empty")

    headers = [_key(c) for c in raw[0]]
    index: dict[int, str] = {i: _COLUMNS[h] for i, h in enumerate(headers)
                             if h in _COLUMNS}
    missing = [name for name in _REQUIRED if name not in index.values()]
    if missing:
        pretty = ", ".join(_HEADER_NAMES[n] for n in missing)
        raise SheetError(
            f"{path}: the header row is missing the {pretty} column(s). "
            f"Use the sheet the tool wrote (--migration-sheets); columns may "
            f"be reordered and extra ones added, but the original headers "
            f"have to survive.")

    sheet = Sheet(source=path)
    for number, values in enumerate(raw[1:], start=2):
        mapping = {index[i]: values[i] for i in index if i < len(values)}
        row = _build_row(mapping, number)
        if not row.old_switch and not row.old_port:
            continue                    # a spacer or a stray note line
        sheet.rows.append(row)
    if not sheet.rows:
        raise SheetError(f"{path}: no data rows found below the header")

    unknown = [h for h in headers if h and h not in _COLUMNS]
    if unknown:
        # not a problem - people add their own columns - but worth saying once
        sheet.problems.append(
            f"{len(unknown)} column(s) not written by this tool were kept and "
            f"ignored")
    return sheet
