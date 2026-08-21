"""Excel (and CSV) export of the report tables.

The workbook is not just a data dump: the cabling sheet is a document that goes
to a data centre, gets written on by hand, and comes back. So the writer takes
the presentation hints each Table carries - which columns a human fills in,
which need wrapping, which are a dropdown - and turns them into a sheet that is
hard to fill in wrongly and readable on paper:

* the columns a technician writes into are shaded, unlocked and wide enough to
  write in, and everything derived from the devices is locked so it cannot be
  edited by accident (no password: this is a guard rail, not a security
  control - the sheet's protection is trivially removable by anyone who needs
  to);
* a NEW switch is picked from a dropdown rather than typed, and the MLT id
  cell refuses anything that is not a whole number;
* wide columns wrap instead of being cut off at the column edge;
* the header row and the identifying columns stay put while scrolling right;
* it prints landscape, fit to width, with the header repeated on every page and
  a page break whenever the old switch changes - so no rack's rows straddle two
  sheets of paper.

A hidden sheet records which tool version wrote the workbook and what the
columns were, so reading a filled-in sheet back can tell a current sheet from
one produced by an older build.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Protection,
    Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.datavalidation import DataValidation

from switch_migrator import __version__
from switch_migrator.report.tables import Table

# Bumped whenever the cabling sheet's columns change in a way that a read-back
# should know about. Recorded in the hidden stamp sheet.
SHEET_SCHEMA = 2
STAMP_SHEET = "_migrator"

_FILLS = {
    "ok": PatternFill("solid", start_color="C6EFCE"),
    "warn": PatternFill("solid", start_color="FFEB9C"),
    "error": PatternFill("solid", start_color="FFC7CE"),
}
_HEADER_FILL = PatternFill("solid", start_color="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
# the columns a human writes into: pale amber, so they are obvious on a colour
# screen and still visibly lighter than everything else on a mono printout
_MANUAL_FILL = PatternFill("solid", start_color="FFF2CC")
_MANUAL_HEADER_FILL = PatternFill("solid", start_color="BF8F00")

_THIN = Side(style="thin", color="BFBFBF")
_GRID = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

# Excel worksheet titles: 31 characters, and these are illegal in them.
# Location names come from the user's config ('Frankfurt DC1/DC2'), so both
# limits are reachable in normal use.
_ILLEGAL_TITLE = re.compile(r"[\[\]:*?/\\]")

# Column widths. Wrapped columns get a fixed, readable width rather than one
# derived from their longest cell (a 14-VLAN pair list would otherwise ask for
# 200 characters and be clipped at the cap anyway).
_WRAP_WIDTH = 34
_MANUAL_WIDTH = 16
_MAX_WIDTH = 46


def _sheet_title(title: str, used: set[str]) -> str:
    clean = _ILLEGAL_TITLE.sub("-", title).strip() or "Sheet"
    clean = clean[:31]
    if clean not in used:
        used.add(clean)
        return clean
    # two long location names can collide once truncated; number them rather
    # than letting openpyxl silently rename or raise
    for n in range(2, 100):
        suffix = f" ({n})"
        candidate = clean[:31 - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
    used.add(clean)
    return clean


def _column_width(table: Table, index: int, header: str) -> int:
    if header in table.wrap_columns:
        return _WRAP_WIDTH
    longest = max([len(str(header))]
                  + [len(str(r[index])) for r in table.rows[:500]], default=0)
    if header in table.manual_columns:
        return max(_MANUAL_WIDTH, min(longest + 2, _MAX_WIDTH))
    return min(longest + 2, _MAX_WIDTH)


def _write_header(ws, table: Table) -> None:
    ws.append(table.headers)
    for index, header in enumerate(table.headers, start=1):
        cell = ws.cell(row=1, column=index)
        cell.fill = (_MANUAL_HEADER_FILL if header in table.manual_columns
                     else _HEADER_FILL)
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center",
                                   wrap_text=True)
        cell.border = _GRID
    ws.row_dimensions[1].height = 30


def _add_validations(ws, table: Table, last_row: int) -> None:
    """Dropdowns and whole-number checks on the columns a human fills in."""
    if last_row < 2:
        return
    for header, choices in table.choice_columns.items():
        if header not in table.headers or not choices:
            continue
        letter = get_column_letter(table.headers.index(header) + 1)
        # an inline list is limited to 255 characters in the file format; past
        # that the dropdown would silently not appear, so fall back to a plain
        # (still helpful) note rather than pretending it is there
        formula = '"' + ",".join(choices) + '"'
        if len(formula) > 255:
            continue
        dv = DataValidation(type="list", formula1=formula, allow_blank=True,
                            showDropDown=False)
        dv.error = "Pick one of the switches this migration targets."
        dv.errorTitle = "Not a target switch"
        dv.prompt = "Pick the switch this link now lands on."
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}{last_row}")

    for header in table.int_columns:
        if header not in table.headers:
            continue
        letter = get_column_letter(table.headers.index(header) + 1)
        dv = DataValidation(type="whole", operator="between", formula1=1,
                            formula2=1000000, allow_blank=True)
        dv.error = "This cell takes a number only."
        dv.errorTitle = "Numbers only"
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}{last_row}")


def _add_page_breaks(ws, table: Table) -> None:
    """One printed page per group, so a rack's rows never span two sheets."""
    if not table.page_break_column or table.page_break_column not in table.headers:
        return
    index = table.headers.index(table.page_break_column)
    previous = None
    for offset, row in enumerate(table.rows):
        value = row[index]
        if previous is not None and value != previous:
            # break BEFORE this row, i.e. after the previous one
            ws.row_breaks.append(Break(id=offset + 1))
        previous = value


def _setup_printing(ws, table: Table) -> None:
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "1:1"
    ws.print_options.horizontalCentered = True
    _add_page_breaks(ws, table)


def _freeze(ws, table: Table) -> None:
    """Keep the header - and the columns that identify a row - on screen."""
    columns = min(table.freeze_columns, len(table.headers))
    ws.freeze_panes = f"{get_column_letter(columns + 1)}2" if columns else "A2"


def _write_table(wb: Workbook, table: Table, used_titles: set[str]) -> None:
    ws = wb.create_sheet(title=_sheet_title(table.title, used_titles))
    _write_header(ws, table)

    manual = {i for i, h in enumerate(table.headers)
              if h in table.manual_columns}
    wrapped = {i for i, h in enumerate(table.headers) if h in table.wrap_columns}
    protect = bool(table.manual_columns)

    for row, severity in zip(table.rows, table.severities):
        ws.append([str(c) if not isinstance(c, (int, float)) else c
                   for c in row])
        excel_row = ws.max_row
        fill = _FILLS.get(severity)
        for index in range(len(table.headers)):
            cell = ws.cell(row=excel_row, column=index + 1)
            cell.border = _GRID
            if index in manual:
                # the technician's columns keep their own shading even on a
                # coloured row - that is where the eye has to land
                cell.fill = _MANUAL_FILL
                if protect:
                    cell.protection = Protection(locked=False)
            elif fill is not None:
                cell.fill = fill
            if index in wrapped:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")

    _freeze(ws, table)
    ws.auto_filter.ref = ws.dimensions
    for index, header in enumerate(table.headers, start=1):
        ws.column_dimensions[get_column_letter(index)].width = \
            _column_width(table, index - 1, header)
    _add_validations(ws, table, ws.max_row)
    _setup_printing(ws, table)
    if protect:
        # a guard rail against overwriting collected data by accident, not a
        # lock: no password, and every fill-in column is left editable
        ws.protection.sheet = True
        ws.protection.formatCells = False
        ws.protection.selectLockedCells = False
        ws.protection.autoFilter = False
        ws.protection.sort = False


def _write_stamp(wb: Workbook, tables: list[Table], meta: dict | None) -> None:
    """A hidden sheet saying which build wrote this workbook, and with which
    columns - so reading a filled-in sheet back can spot version skew instead
    of quietly misreading a renamed column."""
    ws = wb.create_sheet(title=STAMP_SHEET)
    ws.append(["key", "value"])
    rows = {
        "schema": SHEET_SCHEMA,
        "tool_version": __version__,
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    rows.update({str(k): str(v) for k, v in (meta or {}).items()})
    for key, value in rows.items():
        ws.append([key, value])
    for table in tables:
        ws.append([f"columns:{table.title}", "|".join(table.headers)])
    ws.sheet_state = "hidden"


def write_excel(tables: list[Table], path: Path, meta: dict | None = None) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    used_titles: set[str] = set()
    for table in tables:
        _write_table(wb, table, used_titles)
    _write_stamp(wb, tables, meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_csv(tables: list[Table], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for table in tables:
        slug = re.sub(r"[^a-z0-9]+", "_", table.title.lower()).strip("_")
        path = directory / f"{slug}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(table.headers)
            writer.writerows(table.rows)
        written.append(path)
    return written
