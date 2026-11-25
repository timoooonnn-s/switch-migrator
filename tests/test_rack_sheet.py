"""The rack sheet: a print rendering of the filled-in cabling sheet (D6)."""

from pathlib import Path

import pytest

from switch_migrator import cabling_sheet, cli
from switch_migrator.cabling_sheet import CablingRow, Sheet
from switch_migrator.report.rack_sheet import HEADERS, build_rack_sheets


def _row(switch, port, **kw) -> CablingRow:
    return CablingRow(old_switch=switch, old_port=port, **kw)


def test_one_page_per_old_switch_in_faceplate_order_dead_ports_last():
    sheet = Sheet(rows=[
        _row("sw-b", "1/10", new_switch="leaf-1", new_port="1/2", usage="IN USE",
             neighbor="srv-01", kind="access"),
        _row("sw-b", "1/2", new_switch="leaf-1", new_port="1/1", usage="IN USE",
             mlt_id=35, kind="mlt", port_name="esx-07"),
        _row("sw-b", "1/3", usage="UNUSED"),
        _row("sw-a", "7", usage="UNCERTAIN", rack_old="R12"),
    ])
    tables = build_rack_sheets(sheet)
    assert [t.title for t in tables] == ["Rack sw-a (R12)", "Rack sw-b"]
    b = tables[1]
    port, on_it, link, note = (HEADERS.index(h) for h in ("Port", "On it", "Link", "Note"))
    assert [r[port] for r in b.rows] == ["1/2", "1/10", "1/3"]  # dead 1/3 last
    assert "if a cable IS plugged in" in b.rows[2][note]            # R7
    assert b.rows[0][on_it] == '"esx-07"' and b.rows[0][link] == "MLT 35 (old)"
    a_note = tables[0].rows[0][note]
    assert "no new port planned" in a_note and "identify at the rack" in a_note
    assert "read the cable label" in a_note


def test_the_rack_sheet_is_never_read_back_as_a_cabling_sheet(tmp_path):
    sheet = Sheet(rows=[_row("sw", "1/1", new_switch="leaf", new_port="1/1",
                             usage="IN USE")])
    path = cli.write_rack_sheet(sheet, tmp_path)
    with pytest.raises(cabling_sheet.SheetError, match="Old port"):
        cabling_sheet.load(path)


def test_rack_sheet_from_the_command_line(tmp_path):
    csv = tmp_path / "cabling.csv"
    csv.write_text("Old switch,Old port,NEW switch,NEW port,Usage\n"
                   "sw,1/1,leaf,1/48,IN USE\n", encoding="utf-8")
    assert cli.main(["--rack-sheet", str(csv), "-o", str(tmp_path / "out")]) == 0
    assert list((tmp_path / "out").glob("rack-sheet-*.xlsx"))
