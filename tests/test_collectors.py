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
    assert {p: n.sysname for p, n in parse_lldp_neighbors_summary(output).items()} == {
        "1/1": "dvr-01", "1/47": "old-agg-02"}


def test_parse_lldp_neighbors_summary_ip_and_empty_sysname():
    # real VOSS 8.10.9 layout: IP/IPv6 ADDR column, and an EMPTY SYSNAME cell
    # for a server (its REMOTE PORT/SYSDESCR must not bleed into the name)
    output = (
        "LOCAL            IP/IPv6                                  CHASSIS            REMOTE\n"
        "PORT       PROT  ADDR                                     ID                 PORT               SYSNAME       SYSDESCR\n"
        "-----------------------------------------------------------------------------------------------------------------------\n"
        "1/3        LLDP  -                                        a4:00:00:00:00:03  a4:00:00:00:00:13  10/25Gb 2-po~ 235.1.164.2 fw_version:AFW_1\n"
        "1/7        LLDP  10.0.48.192                              --                 a4:00:00:00:00:07                HPE ProLiant DL380 Gen10\n"
        "2/1        LLDP  10.0.148.88                              a4:00:00:00:00:21  2/1                core-s72-q4   VSP-7254XSQ (8.10.9.0)\n"
    )
    n = parse_lldp_neighbors_summary(output)
    assert n["1/3"].sysname == "10/25Gb" and n["1/3"].ip == ""          # truncated name, no IP
    assert n["1/7"].sysname == "" and n["1/7"].ip == "10.0.48.192"      # server: name blank, IP set
    assert n["1/7"].sys_descr == "HPE ProLiant DL380 Gen10"
    assert n["2/1"].sysname == "core-s72-q4" and n["2/1"].ip == "10.0.148.88"


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


def test_media_column_is_filled_even_when_the_state_command_works(
        tmp_path: Path, cfg: Config):
    """`show ... state` carries ADMIN/OPER and the last-change DATE but has no
    DESCRIPTION column, so on its own it leaves the migration sheets' Media
    column blank. The `interface` variant must still be read and merged in."""
    device = tmp_path / "gx-01"
    shutil.copytree(FIXTURES / "voss", device)
    (device / "show_interfaces_gigabitethernet_interface.txt").write_text(
        "                                      Port Interface\n"
        "PORT                               LINK  PORT          PHYSICAL          STATUS\n"
        "NUM      INDEX DESCRIPTION         TRAP  LOCK    MTU   ADDRESS           ADMIN  OPERATE\n"
        "----------------------------------------------------------------------------------\n"
        "1/1      192   10GbSR              true  false   1950  b0:ad:aa:41:b4:01 up     up\n"
        "1/2      193   10GbLR              true  false   1950  b0:ad:aa:41:b4:02 up     down\n"
    )
    target = SwitchTarget("gx-01", "gx-01", Platform.VOSS)
    audit = collect_switch(target, OfflineRunner("gx-01", tmp_path), cfg)
    by_port = {p.port: p for p in audit.ports}
    # state stays authoritative for the port list and the last-change date
    assert set(by_port) >= {"1/1", "1/2", "1/47", "1/48"}
    assert by_port["1/48"].last_change == "05/13/26 15:54:53"
    # ... and the media type now comes across from the interface table
    assert by_port["1/1"].media == "10GbSR"
    assert by_port["1/2"].media == "10GbLR"
    assert by_port["1/47"].media == ""       # not in the second table, no guess


def test_missing_media_table_is_not_reported_as_a_failure(tmp_path: Path,
                                                          cfg: Config):
    """If `state` already delivered the ports, a release that rejects the
    `interface` variant costs only the media column - not a report warning."""
    device = tmp_path / "gx-02"
    shutil.copytree(FIXTURES / "voss", device)
    (device / "show_interfaces_gigabitethernet_interface.txt").write_text(INVALID)
    target = SwitchTarget("gx-02", "gx-02", Platform.VOSS)
    audit = collect_switch(target, OfflineRunner("gx-02", tmp_path), cfg)
    assert audit.ports
    assert not any("gigabitEthernet interface" in w for w in audit.warnings)
    assert not audit.errors


def test_counter_sections_do_not_reset_has_traffic(tmp_path, cfg):
    """`show int gig statistics` prints several sections (traffic, errors) and
    a port appears in each; an all-zero error row must not overwrite a
    non-zero traffic row - that misclassifies a formerly-active port as
    UNUSED and drops it from the commands file."""
    d = tmp_path / "sw1"
    shutil.copytree(FIXTURES / "voss", d)
    (d / "show_interfaces_gigabitethernet_statistics.txt").write_text(
        "Port Stats Interface\n"
        "PORT_NUM IN_OCTETS OUT_OCTETS\n"
        "1/1 123456 654321\n"
        "Port Stats Interface Error\n"
        "PORT_NUM IN_ERROR OUT_ERROR\n"
        "1/1 0 0\n")
    audit = collect_switch(SwitchTarget("sw1", "sw1", Platform.VOSS),
                           OfflineRunner("sw1", tmp_path), cfg, pull_macs=True)
    port = next(p for p in audit.ports if p.port == "1/1")
    assert port.has_traffic is True


def test_an_unparsable_running_config_is_reported_once(tmp_path):
    """The failure path returned before caching, so the second consumer parsed
    the config again and appended a duplicate warning and coverage row."""
    from switch_migrator.collectors import switch as switch_mod
    from switch_migrator.models import Platform, SwitchAudit

    audit = SwitchAudit(name="sw", host="h", platform=Platform.VOSS,
                        reachable=True)
    audit.running_config = "irrelevant"
    cache: dict = {}

    def _boom(_text):
        raise ValueError("unparsable")

    original = switch_mod.voss_config.parse_voss_config
    switch_mod.voss_config.parse_voss_config = _boom
    try:
        switch_mod._config_bindings(audit, cache)
        switch_mod._config_bindings(audit, cache)
    finally:
        switch_mod.voss_config.parse_voss_config = original

    assert len(audit.warnings) == 1
    assert sum(1 for s in audit.sources if s.name == "running-config") == 1
