from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from migration_check.compare import Comparison, LoadedSnapshot, Verdict, compare
from migration_check.model import DeviceState, MacSighting, Platform, PortStatus, Role
from migration_check.portmap import read_port_map
from migration_check.report import render_text, to_json, trend_line
from migration_check.snapshot import Snapshot
from migration_check.watch import StopReason, WatchResult, watch

PC, PRINTER, PHONE = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"


def _mac(device: str, mac: str, port: str) -> MacSighting:
    return MacSighting(device, mac, 10, f"Port:{port}", (port,), role=Role.EDGE)


def _loaded(state: DeviceState, minute: int = 0) -> LoadedSnapshot:
    taken = datetime(2026, 11, 23, 22, minute, tzinfo=UTC)
    return LoadedSnapshot(Snapshot(Path(state.name), (), taken), (state,))


BEFORE = _loaded(
    DeviceState(
        "old-1",
        Platform.BOSS,
        macs=(_mac("old-1", PC, "7"), _mac("old-1", PRINTER, "8"), _mac("old-1", PHONE, "9")),
        ports=tuple(PortStatus("old-1", port, oper_up=True) for port in ("7", "8", "9")),
        vlans={10: "Users"},
    )
)


def _after(*macs: MacSighting, minute: int = 0) -> LoadedSnapshot:
    ports = (
        PortStatus("new-1", "1/41", oper_up=True),
        PortStatus("new-1", "1/42", oper_up=True),
        PortStatus("new-1", "1/43", oper_up=False),
    )
    return _loaded(
        DeviceState("new-1", Platform.FABRIC_ENGINE, macs, (), ports, {10: "Users"}), minute
    )


PORT_MAP = {
    ("old-1", "7"): ("new-1", "1/41"),
    ("old-1", "8"): ("new-1", "1/42"),
    ("old-1", "9"): ("new-1", "1/43"),
}


def test_swapped_cables_are_other_port() -> None:
    after = _after(_mac("new-1", PC, "1/42"), _mac("new-1", PRINTER, "1/41"))
    comparison = compare([BEFORE], after, port_map=PORT_MAP)

    verdicts = {r.before.mac: r.verdict for r in comparison.results}
    assert verdicts == {PC: Verdict.OTHER_PORT, PRINTER: Verdict.OTHER_PORT, PHONE: Verdict.MISSING}
    problems = {c.old[1]: c.problem for c in comparison.port_checks}
    assert problems == {"7": None, "8": None, "9": "new port down"}

    text = render_text(comparison)
    assert "OTHER PORT (2): swapped cables, or the sheet is wrong" in text
    assert "sheet: new-1 port 1/41, found: new-1 port 1/42" in text
    assert "PORT MAP: 3 link(s), 2 new port(s) up, 1 with a problem" in text
    assert "old-1 port 9  ->  new-1 port 1/43: new port down (1 edge MAC(s) before, 0 back" in text
    assert to_json(comparison)["summary"]["other-port"] == 2


def test_without_a_port_map_the_same_moves_are_found() -> None:
    after = _after(_mac("new-1", PC, "1/42"), _mac("new-1", PRINTER, "1/41"))
    comparison = compare([BEFORE], after)
    assert {r.verdict for r in comparison.results} == {Verdict.FOUND, Verdict.MISSING}
    assert "other port" not in render_text(comparison)


def test_read_port_map_from_a_switch_migrator_csv_export(tmp_path: Path) -> None:
    sheet = tmp_path / "Cabling.csv"
    sheet.write_text(
        "Port ID,OLD switch,OLD port,done?,NEW switch,NEW port\n"
        "P0001,old-1,7,x,new-1,Port 1/41\n"
        "P0002,old-1,8,,,\n"  # not migrated yet
        "P0003,old-1,9,,new-1,\n"  # half filled
        "P0004,old-1,7,,new-1,1 / 44\n"  # mapped twice
        ",,,a note,,\n",
        encoding="utf-8-sig",
    )
    port_map = read_port_map(sheet)
    assert port_map.mapping == {("old-1", "7"): ("new-1", "1/41")}
    assert port_map.pending == 1
    assert port_map.problems == (
        "Cabling.csv row 4: new switch or new port is empty",
        "Cabling.csv row 5: old-1 7 is mapped twice (new-1 1/41 and new-1 1/44)",
    )


def test_a_semicolon_separated_export_is_read(tmp_path: Path) -> None:
    """Excel writes the list separator of the machine's locale, and most of
    Europe's is a semicolon. Assuming a comma made the whole file one column,
    which came back as "needs the columns 'Old switch', ..." - an error about
    the header, for a file whose header is fine. switch-migrator's own reader
    sniffs the separator; this one has to agree with it."""
    sheet = tmp_path / "Cabling.csv"
    sheet.write_text(
        "Port ID;OLD switch;OLD port;NEW switch;NEW port\n"
        "P0001;old-1;7;new-1;Port 1/41\n"
        "P0002;old-1;8;;\n",
        encoding="utf-8-sig",
    )
    port_map = read_port_map(sheet)
    assert port_map.mapping == {("old-1", "7"): ("new-1", "1/41")}
    assert port_map.pending == 1
    assert port_map.problems == ()


def test_a_tab_separated_export_is_read(tmp_path: Path) -> None:
    sheet = tmp_path / "Cabling.csv"
    sheet.write_text(
        "OLD switch\tOLD port\tNEW switch\tNEW port\n" "old-1\t7\tnew-1\t1/41\n",
        encoding="utf-8-sig",
    )
    assert read_port_map(sheet).mapping == {("old-1", "7"): ("new-1", "1/41")}


def test_a_port_map_without_its_columns_is_refused(tmp_path: Path) -> None:
    sheet = tmp_path / "map.csv"
    sheet.write_text("from,to\nold-1:7,new-1:1/41\n")
    with pytest.raises(ValueError, match="'Old switch', 'Old port'"):
        read_port_map(sheet)


def _round(*found: str, minute: int) -> Comparison:
    return compare([BEFORE], _after(*(_mac("new-1", mac, "1/41") for mac in found), minute=minute))


def _run_watch(
    comparisons: list[Comparison], tmp_path: Path, max_rounds: int = 10, stable: int = 2
) -> tuple[WatchResult, list[Path], list[float], list[str]]:
    folders: list[Path] = []
    sleeps: list[float] = []
    lines: list[str] = []
    queue = iter(comparisons)
    result = watch(
        take_snapshot=folders.append,
        compare_round=lambda folder: next(queue),
        out=tmp_path,
        interval_seconds=60,
        max_rounds=max_rounds,
        stable_rounds=stable,
        on_round=lambda rounds: lines.append(trend_line(rounds)),
        sleep=sleeps.append,
    )
    return result, folders, sleeps, lines


def test_watch_stops_when_everything_is_found(tmp_path: Path) -> None:
    rounds = [_round(PC, minute=0), _round(PC, PRINTER, PHONE, minute=2)]
    result, folders, sleeps, lines = _run_watch(rounds, tmp_path)
    assert result.reason is StopReason.ALL_FOUND
    assert folders == [tmp_path / "round-01", tmp_path / "round-02"]
    assert sleeps == [60]
    assert lines[0].startswith("round  1  22:00:00  not found    2 of 3  (missing 2,")
    assert lines[1].endswith("back since last round 2")


def test_watch_stops_when_the_count_stops_falling(tmp_path: Path) -> None:
    rounds = [_round(minute=0), _round(PC, minute=2), _round(PC, minute=4), _round(PC, minute=6)]
    result, folders, sleeps, _ = _run_watch(rounds, tmp_path, stable=2)
    assert result.reason is StopReason.STABLE
    assert len(folders) == 4
    assert len(sleeps) == 3


def test_watch_stops_at_the_round_limit_without_a_last_sleep(tmp_path: Path) -> None:
    rounds = [_round(minute=0), _round(PC, minute=2), _round(PC, PRINTER, minute=4)]
    result, _, sleeps, _ = _run_watch(rounds, tmp_path, max_rounds=3)
    assert result.reason is StopReason.ROUND_LIMIT
    assert len(sleeps) == 2
