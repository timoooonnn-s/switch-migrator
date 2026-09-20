"""Migration-day deliverables: port info sheet, cabling sheet, commands."""

import shutil
from pathlib import Path

import pytest

from switch_migrator.collectors.switch import collect_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import (
    UNTAGGED,
    Platform,
    PortState,
    SwitchAudit,
    VlanBinding,
)
from switch_migrator.parsers.common import normalize_mac, parse_mac_table
from switch_migrator.parsers.voss_parsers import parse_mlt_lacp
from switch_migrator.report.migration import (
    assign_port_uids,
    build_cabling,
    build_commands,
    build_port_info,
)

FIXTURES = Path(__file__).parent / "fixtures"

# real VOSS 'show interfaces gigabitEthernet fdb-entry' output ("Port Fdb"):
# VOSS has NO 'show mac-address-table'.
VOSS_MACS = """\
====================================================================================================
                                    Port Fdb
====================================================================================================
VLAN            MAC                           SMLT
ID   STATUS     ADDRESS            INTERFACE  REMOTE
----------------------------------------------------------------------------------------------------
695  learned    00:11:22:00:00:01  Port-1/7      false
695  learned    00:11:22:00:00:02  Port-1/7      false
174  learned    00:11:22:00:00:04  MLT035.s.1/2/23/24 true

c: customer vid   u: untagged-traffic

3 out of 7 entries in all fdb(s) displayed.
"""

# real ERS/BOSS 'show mac-address-table': MAC first, source is 'Port:48'
ERS_MACS = """\
Mac Address Table Aging Time: 300
Number of addresses: 1655

   MAC Address    Vid   Type       Source
----------------- ---- ------- --------------
0011.2200.0011       735 Dynamic Port:7
0011.2200.0012       735 Dynamic Port:8
0011.2200.0013       174 Dynamic Trunk:1
"""


# --------------------------- parsers ---------------------------------------

def test_normalize_mac_forms():
    assert normalize_mac("00:11:22:33:44:55") == "00:11:22:33:44:55"
    assert normalize_mac("0011.2233.4455") == "00:11:22:33:44:55"
    assert normalize_mac("00-11-22-33-44-55") == "00:11:22:33:44:55"


def test_parse_mac_table_voss_port_prefix_and_mlt():
    t = parse_mac_table(VOSS_MACS)
    # 'Port-1/7' must be attributed to port 1/7, not dropped
    assert [m for m, _ in t["1/7"]] == ["00:11:22:00:00:01", "00:11:22:00:00:02"]
    assert t["1/7"][0][1] == 695                      # VLAN column picked up
    assert [m for m, _ in t["name:MLT035.s.1/2/23/24"]] == ["00:11:22:00:00:04"]
    # the footer line carries no MAC and must not create an entry
    assert all(not k.startswith("2 ") for k in t)


def test_parse_mac_table_ers_bare_ports_and_trunk():
    t = parse_mac_table(ERS_MACS)
    assert [m for m, _ in t["7"]] == ["00:11:22:00:00:11"]
    assert [m for m, _ in t["8"]] == ["00:11:22:00:00:12"]
    assert "mlt:1" in t                                # 'Trunk 1'


def test_parse_mlt_lacp_two_line_header(fixture):
    # real VOSS header spans two lines ('... LACP LACP' then 'MLTID IFINDEX ...')
    lacp = parse_mlt_lacp(fixture("voss", "show_mlt.txt"))
    assert lacp, "LACP table must be found despite the two-line header"
    assert set(lacp.values()) <= {True, False}


# --------------------------- sheets ----------------------------------------

def _audit_with_ports() -> SwitchAudit:
    a = SwitchAudit(name="old-01", host="old-01", platform=Platform.VOSS,
                    reachable=True)
    a.ports = [
        PortState(port="1/1", description="10GbSR", admin_up=True, oper_up=True,
                  lldp_neighbor="srv-a", macs=["00:11:22:00:00:01"], mac_total=1,
                  vlans=[695], isids=[2500695], tagging="untagged",
                  bindings=[VlanBinding(vlan=695, isid=2500695, tagging=UNTAGGED,
                                        source="running-config")]),
        PortState(port="1/2", description="10GbSR", admin_up=True, oper_up=False),
        PortState(port="1/48", description="10GbSR", admin_up=True, oper_up=True,
                  lldp_neighbor="core-01", is_uplink=True, mlt_id=1,
                  mlt_name="uplink", lacp=True),
    ]
    return a


def test_uids_are_sequential_and_stable():
    a, b = _audit_with_ports(), _audit_with_ports()
    b.name = "aaa-00"
    assign_port_uids([a, b])
    # sorted by switch name: aaa-00 first
    assert [p.uid for p in b.ports] == ["P0001", "P0002", "P0003"]
    assert [p.uid for p in a.ports] == ["P0004", "P0005", "P0006"]


def test_port_info_lists_every_port_with_migration_columns():
    a = _audit_with_ports()
    assign_port_uids([a])
    t = build_port_info([a])
    assert len(t.rows) == 3                       # ALL ports, incl. the down one
    hdr = t.headers
    row = t.rows[0]
    assert row[hdr.index("Port ID")] == "P0001"
    assert row[hdr.index("Device on port")] == "srv-a"
    assert row[hdr.index("MAC addresses")] == "00:11:22:00:00:01"
    assert row[hdr.index("VLAN IDs")] == "695"
    assert row[hdr.index("I-SIDs")] == "2500695"
    assert row[hdr.index("VLAN -> I-SID")] == "695->2500695 (u)"
    assert row[hdr.index("VLAN source")] == "running-config"
    assert row[hdr.index("Media")] == "10GbSR"
    uplink = t.rows[2]
    assert uplink[hdr.index("LACP")] == "yes"
    assert uplink[hdr.index("MLT ID")] == 1


def test_cabling_sheet_only_connected_ports_with_blank_new_columns():
    a = _audit_with_ports()
    assign_port_uids([a])
    t = build_cabling([a])
    # 1/2 is down with no neighbor/MAC -> not something to re-patch
    assert len(t.rows) == 2
    hdr = t.headers
    first = t.rows[0]
    # the leading column is the port's UNTAGGED VLAN, not whichever VLAN
    # happened to sort first - on a trunk that number meant nothing
    assert first[hdr.index("Untagged VLAN")] == 695
    assert first[hdr.index("VLAN -> I-SID")] == "695->2500695 (u)"
    assert first[hdr.index("Rack (old)")] == ""        # filled in by hand
    assert first[hdr.index("Type")] == "access"
    assert first[hdr.index("Port ID")] == "P0001"
    assert first[hdr.index("Old switch")] == "old-01"
    assert first[hdr.index("Old port")] == "1/1"
    # the technician fills these in
    assert first[hdr.index("NEW switch")] == ""
    assert first[hdr.index("NEW port")] == ""
    assert t.rows[1][hdr.index("Type")] == "uplink"


def test_mac_cap_shows_overflow_count():
    a = _audit_with_ports()
    a.ports[0].macs = [f"00:11:22:00:00:{i:02x}" for i in range(10)]
    a.ports[0].mac_total = 37
    assign_port_uids([a])
    cell = build_port_info([a]).rows[0][build_port_info([a]).headers
                                        .index("MAC addresses")]
    assert cell.endswith("(+27 more)")


def test_commands_reference_new_switch_and_expected_macs():
    a = _audit_with_ports()
    assign_port_uids([a])
    text = build_commands([a], new_switch="new-01")
    assert "new-01" in text
    assert "show interfaces gigabitEthernet fdb-entry <NEW-PORT>" in text
    assert "P0001" in text and "00:11:22:00:00:01" in text
    assert "was old-01 1/1" in text


# --------------------------- end to end ------------------------------------

def test_collector_attributes_macs_and_lacp_from_real_capture(tmp_path):
    dev = tmp_path / "gx-11-s72-p1"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    (dev / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text(VOSS_MACS)

    cfg = Config(dvr_controllers=[], core_switch_patterns=["gx-11-s74-*"],
                 isid_offsets=[2500000], isid_explicit={}, excluded_vlans=set(),
                 ssh=SshSettings())
    # MAC/optic collection is opt-in (pull_macs) so plain audits stay fast
    audit = collect_switch(
        SwitchTarget("gx-11-s72-p1", "gx-11-s72-p1", Platform.VOSS),
        OfflineRunner("gx-11-s72-p1", tmp_path), cfg, pull_macs=True)
    by_port = {p.port: p for p in audit.ports}
    # port MACs land on the right port ('Port-1/7')
    assert by_port["1/7"].macs == ["00:11:22:00:00:01", "00:11:22:00:00:02"]
    # the MLT-35 MAC fans out to its member ports
    assert "00:11:22:00:00:04" in by_port["1/1"].macs
    # LACP comes from the show mlt LACP table (MLT 196 is enabled there)
    assert by_port["1/10"].lacp is True
    assert by_port["1/1"].lacp is False        # MLT 35: LACP disabled


def test_macs_not_collected_unless_requested(tmp_path):
    # a plain audit must not send 'show mac-address-table' (it can be a large
    # output on a busy switch) - the MLT/VLAN/LACP derivations still happen
    dev = tmp_path / "gx-11-s72-p1"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    (dev / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text(VOSS_MACS)

    cfg = Config(dvr_controllers=[], core_switch_patterns=[], isid_offsets=[],
                 isid_explicit={}, excluded_vlans=set(), ssh=SshSettings())
    audit = collect_switch(
        SwitchTarget("gx-11-s72-p1", "gx-11-s72-p1", Platform.VOSS),
        OfflineRunner("gx-11-s72-p1", tmp_path), cfg)          # pull_macs=False
    by_port = {p.port: p for p in audit.ports}
    assert by_port["1/7"].macs == []
    # the free derivations are still there
    assert by_port["1/1"].mlt_id == 35
    assert by_port["1/1"].lacp is False


def test_fdb_per_port_fallback_when_bare_form_rejected(tmp_path):
    # some releases insist on a port argument. The bare command then fails and
    # the collector must fall back to asking each UP port individually.
    dev = tmp_path / "sw"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    invalid = "                     ^\n% Invalid input detected at '^' marker.\n"
    (dev / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text(invalid)
    # per-port captures for two of the up ports (1/7 and 1/10)
    (dev / "show_interfaces_gigabitethernet_fdb_entry_1_7.txt").write_text(
        "VLAN ID   STATUS     ADDRESS            INTERFACE  REMOTE\n"
        "695  learned    00:11:22:00:00:aa  Port-1/7      false\n")
    (dev / "show_interfaces_gigabitethernet_fdb_entry_1_10.txt").write_text(
        "VLAN ID   STATUS     ADDRESS            INTERFACE  REMOTE\n"
        "735  learned    00:11:22:00:00:bb  Port-1/10     false\n")

    cfg = Config(dvr_controllers=[], core_switch_patterns=[], isid_offsets=[],
                 isid_explicit={}, excluded_vlans=set(), ssh=SshSettings())
    audit = collect_switch(SwitchTarget("sw", "sw", Platform.VOSS),
                           OfflineRunner("sw", tmp_path), cfg, pull_macs=True)
    by_port = {p.port: p for p in audit.ports}
    assert by_port["1/7"].macs == ["00:11:22:00:00:aa"]
    assert by_port["1/10"].macs == ["00:11:22:00:00:bb"]
    # the failed bare attempt and the missing per-port captures stay quiet
    assert not [w for w in audit.warnings if "fdb-entry" in w]


def test_mlt_vlans_parsed_from_show_mlt_incl_continuation(fixture):
    from switch_migrator.parsers.voss_parsers import parse_mlt
    # the VLAN IDS column of the Mlt Info table, incl. lines that wrap
    mlts = {m.mlt_id: m for m in parse_mlt(
        "38  6181  s129  trunk  smlt  smlt  1/29   174 695 735\n"
        "2246 2901 2952\n"
        "2600\n")}
    assert mlts[38].vlans == [174, 695, 735, 2246, 2901, 2952, 2600]
    assert mlts[38].members == ["1/29"]


def test_cabling_sheet_carries_mlt_and_port_vlans(tmp_path):
    dev = tmp_path / "sw"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    (dev / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text(VOSS_MACS)
    cfg = Config(dvr_controllers=[], core_switch_patterns=[], isid_offsets=[],
                 isid_explicit={}, excluded_vlans=set(), ssh=SshSettings())
    audit = collect_switch(SwitchTarget("sw", "sw", Platform.VOSS),
                           OfflineRunner("sw", tmp_path), cfg, pull_macs=True)
    assign_port_uids([audit])
    t = build_cabling([audit])
    hdr = t.headers
    for col in ("MLT ID", "MLT name", "MLT VLANs", "MLT I-SIDs",
                "Port VLANs", "Port I-SIDs"):
        assert col in hdr, col
    # 1/1 is a member of MLT 35, which carries 34 VLANs on this box
    row = next(r for r in t.rows if r[hdr.index("Old port")] == "1/1")
    assert row[hdr.index("MLT ID")] == 35
    assert row[hdr.index("MLT name")] == "MLT035.s.1/2/23/24"
    mlt_vlans = row[hdr.index("MLT VLANs")].split(",")
    assert "174" in mlt_vlans and len(mlt_vlans) == 34
    assert row[hdr.index("MLT I-SIDs")]          # mapped through the VLAN table
    # a row with no MLT leaves those cells blank rather than inventing data
    non_mlt = [r for r in t.rows if r[hdr.index("MLT ID")] == ""]
    assert all(r[hdr.index("MLT VLANs")] == "" for r in non_mlt)


def test_real_optics_columns_drop_the_ddm_flag():
    # real 'show pluggable-optical-modules basic':
    #   PORT NUM | TYPE | DDM SUPPORTED | VENDOR NAME | PART NUMBER | SKU
    # the TRUE/FALSE DDM flag must not end up in the transceiver text
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp())
    dev = tmp / "sw"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    (dev / "show_pluggable_optical_modules_basic.txt").write_text(
        "PORT                  DDM\n"
        "NUM    TYPE           SUPPORTED          VENDOR NAME        PART NUMBER\n"
        "-----------------------------------------------------------------------\n"
        "1/7    10GbSR         TRUE               Extreme            10301\n")
    cfg = Config(dvr_controllers=[], core_switch_patterns=[], isid_offsets=[],
                 isid_explicit={}, excluded_vlans=set(), ssh=SshSettings())
    audit = collect_switch(SwitchTarget("sw", "sw", Platform.VOSS),
                           OfflineRunner("sw", tmp), cfg, pull_macs=True)
    t = {p.port: p.transceiver for p in audit.ports}
    assert t["1/7"] == "10GbSR Extreme 10301"
    assert "TRUE" not in t["1/7"]


def test_fdb_mlt_name_fans_out_to_member_ports(tmp_path):
    # real VOSS fdb prints the MLT's NAME in the INTERFACE column; it must be
    # resolved against the known MLTs, not filed under a bogus port
    dev = tmp_path / "sw"
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", dev)
    (dev / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text(VOSS_MACS)
    cfg = Config(dvr_controllers=[], core_switch_patterns=[], isid_offsets=[],
                 isid_explicit={}, excluded_vlans=set(), ssh=SshSettings())
    audit = collect_switch(SwitchTarget("sw", "sw", Platform.VOSS),
                           OfflineRunner("sw", tmp_path), cfg, pull_macs=True)
    by_port = {p.port: p for p in audit.ports}
    # MLT035 members are 1/1,1/2,1/23,1/24 - all get the MLT-learned MAC
    for member in ("1/1", "1/2", "1/23", "1/24"):
        assert "00:11:22:00:00:04" in by_port[member].macs, member
    # and nothing was filed under the VLAN id as if it were a port
    assert "31" not in by_port and "174" not in by_port


def test_cabling_sheet_has_new_mlt_placeholders():
    a = _audit_with_ports()
    assign_port_uids([a])
    t = build_cabling([a])
    hdr = t.headers
    for col in ("NEW switch", "NEW port", "NEW MLT ID", "NEW MLT name", "NEW VLAN"):
        assert col in hdr, col
        assert all(r[hdr.index(col)] == "" for r in t.rows), f"{col} must be blank"


def test_per_location_cabling_sheets_stay_out_of_the_console():
    """--split-by-location titles the sheets 'Cabling <group>'; without -v the
    21-column fill-in sheets must not be dumped to the terminal."""
    import io
    from rich.console import Console
    from switch_migrator.report import console as console_report
    from switch_migrator.report.tables import Table

    t = Table("Cabling Frankfurt", ["A"])
    t.add(["x"])
    buf = io.StringIO()
    console_report.render([t], Console(file=buf, width=100), verbose=False)
    assert buf.getvalue() == ""            # file-only without -v
    buf = io.StringIO()
    console_report.render([t], Console(file=buf, width=100), verbose=True)
    assert "x" in buf.getvalue()           # -v shows it
