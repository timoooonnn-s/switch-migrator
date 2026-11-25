"""The verdicts on a hand-built BOSS -> Fabric Engine consolidation (synthetic values)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from migration_check.compare import Comparison, LoadedSnapshot, Verdict, compare
from migration_check.model import DeviceState, MacSighting, Neighbor, Platform, Role
from migration_check.snapshot import Snapshot

PC, PHONE, PRINTER, MOVED, NEW_PC, QUIET = (f"00:00:00:00:00:0{i}" for i in range(1, 7))


def _mac(
    device: str, mac: str, vlan: int, port: str, role: Role = Role.EDGE, **kw: str
) -> MacSighting:
    return MacSighting(device, mac, vlan, f"Port:{port}", (port,), role=role, **kw)  # type: ignore[arg-type]


def _snapshot(day: int, *devices: DeviceState) -> LoadedSnapshot:
    taken = datetime(2026, 11, day, 20, 0, tzinfo=UTC)
    return LoadedSnapshot(Snapshot(Path(f"day{day}"), (), taken, label=f"day{day}"), devices)


def _before(day: int, *macs: MacSighting) -> LoadedSnapshot:
    phone = Neighbor("old-1", "7", "00:00:00:00:00:02", "port 1", "SEP-phone", "BT")
    uplink = Neighbor("old-1", "49", "b0:00:00:00:00:01", "1/1", "dvr-01", "rB")
    return _snapshot(
        day,
        DeviceState(
            "old-1",
            Platform.BOSS,
            macs=macs,
            neighbors=(phone, uplink),
            vlans={735: "Users", 200: "Printers", 300: "Legacy"},
            ip_by_mac={PC: "10.7.35.10"},
        ),
    )


def _after() -> LoadedSnapshot:
    phone = Neighbor("new-1", "1/41", "00:00:00:00:00:02", "port 1", "SEP-phone", "BT")
    return _snapshot(
        23,
        DeviceState(
            "new-1",
            Platform.FABRIC_ENGINE,
            macs=(
                _mac("new-1", PC, 735, "1/41"),
                _mac("new-1", PHONE, 1735, "1/41"),  # renumbered with --vlan-map
                _mac("new-1", PRINTER, 999, "1/12"),
                _mac("new-1", MOVED, 735, "1/2", Role.FABRIC, remote_beb="fe-07"),
                _mac("new-1", NEW_PC, 735, "1/13"),
            ),
            neighbors=(phone,),
            vlans={735: "Users", 1735: "Voice", 200: "Printers", 999: "quarantine"},
        ),
    )


def _comparison() -> Comparison:
    day_before = _before(22, _mac("old-1", QUIET, 735, "9"))
    window = _before(
        23,
        _mac("old-1", PC, 735, "7"),
        _mac("old-1", PHONE, 735, "7"),
        _mac("old-1", PRINTER, 200, "12"),
        _mac("old-1", MOVED, 735, "8"),
        _mac("old-1", "00:00:00:00:0b:01", 735, "49", Role.UPLINK),  # never compared
    )
    # given out of order on purpose: the baseline is merged by time
    return compare([window, day_before], _after(), vlan_map={735: 735})


def _verdicts(comparison: Comparison) -> dict[str, Verdict]:
    return {result.before.mac: result.verdict for result in comparison.results}


def test_every_verdict() -> None:
    assert _verdicts(_comparison()) == {
        PC: Verdict.FOUND,
        PHONE: Verdict.WRONG_VLAN,  # 735 -> 1735 not declared in this comparison
        PRINTER: Verdict.WRONG_VLAN,  # NAC quarantine
        MOVED: Verdict.ELSEWHERE,
        QUIET: Verdict.MISSING,  # only in the day-before snapshot
    }


def test_a_vlan_map_turns_a_renumbered_vlan_into_found() -> None:
    comparison = compare([_before(23, _mac("old-1", PHONE, 735, "7"))], _after(), {735: 1735})
    assert _verdicts(comparison) == {PHONE: Verdict.FOUND}
    assert comparison.results[0].expected_vlan == 1735


def test_results_carry_ip_lldp_neighbor_and_where_it_went() -> None:
    by_mac = {result.before.mac: result for result in _comparison().results}
    assert by_mac[PC].ip == "10.7.35.10"
    assert by_mac[PC].neighbor == "SEP-phone"  # the phone on the old port, not the uplink
    assert [s.location for s in by_mac[MOVED].after] == ["new-1 via fe-07"]
    assert [s.vlan_id for s in by_mac[PRINTER].after] == [999]


def test_new_edge_macs_and_neighbours_are_keyed_by_identity() -> None:
    comparison = _comparison()
    assert [s.mac for s in comparison.new_macs] == [NEW_PC]
    # the phone moved from port 7 to 1/41 but advertises the same: not lost
    assert [n.label for n in comparison.lost_neighbors] == ["dvr-01"]
    assert comparison.new_neighbors == ()


def test_a_vlan_that_exists_nowhere_after() -> None:
    comparison = compare([_before(23, _mac("old-1", PC, 300, "7"))], _after())
    assert [(g.vlan_id, g.name, g.edge_macs) for g in comparison.vlans_gone] == [(300, "Legacy", 1)]
    assert not comparison.clean


def test_clean_only_when_everything_is_found() -> None:
    assert compare([_before(23, _mac("old-1", PC, 735, "7"))], _after()).clean
    assert not _comparison().clean
