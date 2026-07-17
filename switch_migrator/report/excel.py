"""Excel (and CSV) export of the report tables."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from switch_migrator.report.tables import Table

_FILLS = {
    "ok": PatternFill("solid", start_color="C6EFCE"),
    "warn": PatternFill("solid", start_color="FFEB9C"),
    "error": PatternFill("solid", start_color="FFC7CE"),
}
_HEADER_FILL = PatternFill("solid", start_color="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def write_excel(tables: list[Table], path: Path) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for table in tables:
        ws = wb.create_sheet(title=table.title[:31])
        ws.append(table.headers)
        for cell in ws[1]:
            cell.fill = _HEADER_FILL
            cell.font = _HEADER_FONT
            cell.alignment = Alignment(vertical="center")
        for row, severity in zip(table.rows, table.severities):
            ws.append([str(c) if not isinstance(c, (int, float)) else c
                       for c in row])
            if severity in _FILLS:
                for cell in ws[ws.max_row]:
                    cell.fill = _FILLS[severity]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for idx, header in enumerate(table.headers, start=1):
            width = max([len(str(header))] +
                        [len(str(r[idx - 1])) for r in table.rows[:500]])
            ws.column_dimensions[get_column_letter(idx)].width = min(width + 2, 60)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_csv(tables: list[Table], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for table in tables:
        slug = re.sub(r"[^a-z0-9]+", "_", table.title.lower()).strip("_")
        path = directory / f"{slug}.csv"
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(table.headers)
            writer.writerows(table.rows)
        written.append(path)
    return written
