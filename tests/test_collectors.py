"""Collector-level behavior: fallback chains, partial-DvR handling."""

import shutil
from pathlib import Path

import pytest

from switch_migrator.collectors.dvr import collect_dvr
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import FabricState, Platform
from switch_migrator.parsers.common import parse_lldp_neighbors_summary

FIXTURES = Path(__file__).parent / "fixtures"

INVALID = "                     ^\n% Invalid input detected at '^' marker.\n"


@pytest.fixture
def cfg() -> Config:
    return Config(
        dvr_controllers=[],
        core_switch_patterns=["dvr-*", "core-*"],
        isid_offsets=[10000, 20000],
        isid_explicit={},
        excluded_vlans={1, 4000},
        ssh=SshSettings(),
    )


def test_dvr_without_fabric_wide_data_is_not_authoritative(tmp_path: Path):
    # only the local attachment commands succeed -> the controller must NOT
    # count as an authoritative fabric source (would cause false MISSING)
    device = tmp_path / "dvr-01"
    device.mkdir()
    shutil.copy(FIXTURES / "voss" / "show_vlan_i_sid.txt",
                device / "show_vlan_i_sid.txt")
    shutil.copy(FIXTURES / "voss" / "show_i_sid.txt", device / "show_i_sid.txt")

    fabric = FabricState()
    collect_dvr("dvr-01", OfflineRunner("dvr-01", tmp_path), fabric)
    assert fabric.dvrs_ok == []
    assert any("not counted as an authoritative" in e for e in fabric.dvr_errors)
    # the partial data itself is still merged (harmless extra attachments)
    assert 10100 in fabric.isids


def test_voss_port_fallback_chain_and_lldp_warning(tmp_path: Path, cfg: Config):
    # box rejects `state` and plain `show int gig` (real capture behavior),
    # accepts the `interface` subcommand; LLDP rejected entirely
    device = tmp_path / "old-agg-01"
    shutil.copytree(FIXTURES / "voss", device)
    (device / "show_interfaces_gigabitethernet_state.txt").write_text(INVALID)
    plain = (device / "show_interfaces_gigabitethernet.txt").read_text()
    (device / "show_interfaces_gigabitethernet.txt").write_text(INVALID)
    (device / "show_interfaces_gigabitethernet_interface.txt").write_text(plain)
    (device / "show_lldp_neighbor.txt").write_text(INVALID)
    (device / "show_lldp_neighbor_summary.txt").write_text(INVALID)

    target = SwitchTarget("old-agg-01", "old-agg-01", Platform.VOSS)
    audit = collect_switch(target, OfflineRunner("old-agg-01", tmp_path), cfg)
    assert audit.ports, "third command in the chain must have delivered ports"
    assert audit.ports_up == 3
    assert not audit.errors
    assert any("no LLDP neighbor data" in w for w in audit.warnings)


def test_parse_lldp_neighbors_summary():
    output = (
        "Port     ChassisId           PortId        SysName          SysCap\n"
        "-------  ------------------  ------------  ---------------  ------\n"
        "1/1      b0:ad:aa:41:b4:df   1/47          dvr-01           rB/rB\n"
        "1/47     b0:ad:aa:41:c2:aa   1/48          old-agg-02       rB/rB\n"
    )
    assert parse_lldp_neighbors_summary(output) == {
        "1/1": "dvr-01", "1/47": "old-agg-02"}


def test_ers_unsupported_commands_stay_out_of_the_report(tmp_path: Path, cfg: Config):
    # real access-ERS behavior: 'show ist' and 'show lldp neighbor summary'
    # don't exist on the box (Invalid input). The audit must stay quiet about
    # them - block-form LLDP is the native command and is tried first.
    device = tmp_path / "old-access-01"
    shutil.copytree(FIXTURES / "ers", device)
    (device / "show_ist.txt").write_text(INVALID)

    target = SwitchTarget("old-access-01", "old-access-01", Platform.ERS)
    audit = collect_switch(target, OfflineRunner("old-access-01", tmp_path), cfg)
    assert audit.ist is None
    assert not audit.errors
    assert not [w for w in audit.warnings if "show ist" in w]
    assert not [w for w in audit.warnings if "summary" in w]
    # block-form LLDP delivered neighbors on the first try
    assert any(p.lldp_neighbor for p in audit.ports)
