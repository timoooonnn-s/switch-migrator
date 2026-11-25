from __future__ import annotations

from pathlib import Path

import pytest

from mac_check_samples import BOSS_MAC_SAMPLE, ERS_CAPTURE
from migration_check.model import Platform, Role
from migration_check.platforms import boss
from migration_check.roles import assign_roles
from migration_check.snapshot import RawDevice


def _device(outputs: dict[str, str]) -> RawDevice:
    return RawDevice("old-access-01", Platform.BOSS, outputs)


def _from_switch_migrator(folder: Path, mac_table: str = BOSS_MAC_SAMPLE) -> RawDevice:
    outputs = {file.stem: file.read_text() for file in folder.glob("*.txt")}
    outputs["show_mac_address_table"] = mac_table
    return _device(outputs)


def test_work_fixtures_read_completely_except_the_cut_mac_table() -> None:
    state = assign_roles(boss.read_device(_from_switch_migrator(ERS_CAPTURE)))

    assert [(m.mac, m.vlan_id, m.ports, m.mlt_id, m.role) for m in state.macs] == [
        ("00:11:22:00:00:11", 735, ("7",), None, Role.EDGE),
        ("00:11:22:00:00:12", 735, ("8",), None, Role.EDGE),
        # Trunk:1 is MLT 1 on 49-50, where LLDP sees the DvR controllers (rB)
        ("00:11:22:00:00:13", 174, ("49", "50"), 1, Role.UPLINK),
    ]
    assert [(n.local_port, n.system_name, n.capabilities) for n in state.neighbors] == [
        ("49", "dvr-01", "rB"),
        ("50", "dvr-02", "rB"),
    ]
    assert state.neighbors[0].chassis_id == "b0:ad:aa:41:b4:df"
    assert state.vlans[100] == "Users Floor 1"
    assert len(state.vlans) == 5
    assert (state.ports_up, len(state.ports)) == (3, 5)
    assert state.problems == (
        "'show mac-address-table': the switch reports 1655 addresses, 3 were read",
    )


def test_mlt_rows_keep_names_with_spaces_and_drop_disabled_slots() -> None:
    text = (ERS_CAPTURE / "show_mlt.txt").read_text()
    # MLT 2 is enabled without members, MLT 3 disabled: neither can carry a MAC
    assert boss.parse_mlts(text, []) == {1: ("49", "50"), 6: ("47", "48")}


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("NONE", ()),
        ("1-3,7", ("1", "2", "3", "7")),
        ("1/1-1/3,2/5", ("1/1", "1/2", "1/3", "2/5")),
        ("1/7-9", ("1/7", "1/8", "1/9")),
    ],
)
def test_expand_ports(printed: str, expected: tuple[str, ...]) -> None:
    assert boss.expand_ports(printed) == expected


def test_a_range_across_stack_units_is_reported_not_guessed() -> None:
    problems: list[str] = []
    assert boss.expand_ports("1/47-2/2", problems) == ("1/47", "2/2")
    assert problems == ["port range '1/47-2/2' spans stack units; not expanded"]


def test_stack_ports_and_two_entries_on_one_line() -> None:
    table = (
        "Number of addresses: 3\n"
        "0011.2200.0021  10 Dynamic Port:2/7     0011.2200.0022  10 Dynamic Port:3/1\n"
        "0011.2200.0023  10 Dynamic Port:1/48\n"
    )
    state = boss.read_device(_device(_required(mac_table=table)))
    assert [m.ports for m in state.macs] == [("2/7",), ("3/1",), ("1/48",)]
    assert state.problems == ()


def test_a_mac_line_that_does_not_fit_is_reported() -> None:
    table = "0011.2200.0031  Port:7\n0011.2200.0032  10 Dynamic Cpu:1\n"
    problems = boss.read_device(_device(_required(mac_table=table))).problems
    assert problems == (
        "'show mac-address-table': line not read: '0011.2200.0031  Port:7' "
        "(a MAC without VID, type and source)",
        "'show mac-address-table': line not read: '0011.2200.0032  10 Dynamic Cpu:1' "
        "(unknown source 'Cpu:1')",
    )


def test_a_rejected_command_is_a_problem_not_an_empty_table() -> None:
    outputs = _required(mac_table="% Invalid input detected at '^' marker.\n")
    state = boss.read_device(_device(outputs))
    assert state.macs == ()
    assert state.problems == (
        "'show mac-address-table': switch reported: % Invalid input detected at '^' marker.",
    )


def test_missing_commands_are_named() -> None:
    state = boss.read_device(_device({}))
    assert len(state.problems) == len(boss.COMMANDS)
    assert "'show mac-address-table' not in the snapshot" in state.problems


def _required(mac_table: str) -> dict[str, str]:
    return {
        "show_mac_address_table": mac_table,
        "show_lldp_neighbor": "",
        "show_mlt": "",
        "show_vlan": "",
        "show_interfaces": "",
    }
