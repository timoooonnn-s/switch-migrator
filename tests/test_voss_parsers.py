from switch_migrator.parsers import voss_parsers
from switch_migrator.parsers.common import parse_lldp_neighbors


def test_parse_ports(fixture):
    # full `show interfaces gigabitEthernet` output with Port Interface,
    # Port Name and Port Config sections - only the first may be parsed
    ports = voss_parsers.parse_ports(
        fixture("voss", "show_interfaces_gigabitethernet.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1/1"].admin_up is True and by_port["1/1"].oper_up is True
    assert by_port["1/2"].admin_up is True and by_port["1/2"].oper_up is False
    assert by_port["1/48"].admin_up is False
    assert by_port["2/1/1"].oper_up is True  # channelized port
    # descriptions come from the Port Interface section, not Port Name
    assert by_port["1/1"].description == "1000BaseTX"


def test_parse_ports_without_section_banner(fixture):
    # partial capture: just the data lines, no banners -> still parses
    full = fixture("voss", "show_interfaces_gigabitethernet.txt")
    section = full.split("Port Name")[0]
    data_only = "\n".join(l for l in section.splitlines() if l[:1].isdigit())
    ports = voss_parsers.parse_ports(data_only)
    assert len(ports) == 5


def test_parse_mlt(fixture):
    # full real-world output: 4 tables, footers, wrapped VLAN IDS column
    mlts = voss_parsers.parse_mlt(fixture("voss", "show_mlt.txt"))
    # exactly one entry per MLT: the LACP / port-members / ENCAP tables and
    # the 'All N out of M' footers must not create extra or duplicate rows
    assert [m.mlt_id for m in mlts] == [1, 2, 10]
    ist = mlts[0]
    assert ist.is_ist
    assert ist.members == ["1/47", "1/48"]
    assert mlts[1].name == "MLT002.s.1/1/2"  # names may contain slashes
    assert mlts[1].members == ["1/1", "1/2"]  # trailing VLAN IDS column ignored
    assert mlts[1].admin == "smlt"
    assert mlts[2].mlt_type == "access"
    assert mlts[2].members == ["2/1/1"]


def test_parse_mlt_wrapped_vlan_ids_continuation():
    # a wrapped VLAN IDS continuation line must never become an MLT,
    # regardless of how many VLAN ids it carries
    output = (
        "38  6181  s129         trunk   smlt   smlt     1/29              174 695 735\n"
        "2246 2901 2952\n"
        "2600\n"
    )
    mlts = voss_parsers.parse_mlt(output)
    assert [m.mlt_id for m in mlts] == [38]
    assert mlts[0].members == ["1/29"]


def test_parse_port_state(fixture):
    ports = voss_parsers.parse_port_state(
        fixture("voss", "show_interfaces_gigabitethernet_state.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1/1"].admin_up is True and by_port["1/1"].oper_up is True
    assert by_port["1/2"].admin_up is True and by_port["1/2"].oper_up is False
    assert by_port["1/48"].admin_up is False
    assert by_port["1/48"].state_reason == "SSH"
    assert by_port["1/47"].state_reason == ""
    assert by_port["2/1/1"].oper_up is True


def test_parse_port_isid(fixture):
    rows = voss_parsers.parse_port_isid(
        fixture("voss", "show_interfaces_gigabitethernet_i_sid.txt"))
    assert rows == [
        {"port": "1/1", "isid": 10100, "vlan": 100},
        {"port": "1/2", "isid": 10200, "vlan": 200},
        {"port": "2/1/1", "isid": 77777, "vlan": 300},  # CVLAN row
    ]


def test_parse_virtual_ist(fixture):
    ist = voss_parsers.parse_virtual_ist(fixture("voss", "show_virtual_ist.txt"))
    assert ist is not None
    assert ist.peer_ip == "192.168.255.2"
    assert ist.vlan == 4000
    assert ist.enabled is True
    assert ist.session_up is True


def test_parse_virtual_ist_absent():
    assert voss_parsers.parse_virtual_ist("no data") is None


def test_parse_vlan_isid(fixture):
    vlans = voss_parsers.parse_vlan_isid(fixture("voss", "show_vlan_i_sid.txt"))
    by_id = {v.vlan_id: v for v in vlans}
    # footer '5 out of 5 Total ...' (real format, no leading 'All') must not
    # become VLAN 5
    assert set(by_id) == {1, 100, 200, 300, 4000}
    assert by_id[100].isid == 10100
    # the third column is the I-SID name, kept separate from the VLAN name
    # (which comes from 'show vlan basic')
    assert by_id[100].isid_name == "Server-VLAN-100"
    assert by_id[100].name == ""
    assert by_id[200].isid == 10200
    assert by_id[300].isid == 77777
    assert by_id[300].isid_name == "quarantaine"
    assert by_id[1].isid is None
    assert by_id[4000].isid is None


def test_parse_isid_local(fixture):
    isids = voss_parsers.parse_isid_local(fixture("voss", "show_i_sid.txt"))
    # footer '4 out of 4 Total ...' must not become I-SID 4
    assert set(isids) == {10100, 10200, 20300, 2502201}
    assert isids[10100]["cvids"] == {100}
    assert isids[10200]["cvids"] == {200}
    assert isids[20300]["cvids"] == {300}
    assert isids[10100]["name"] == "Server-100"
    # CVLAN type rows and lowercase names must work
    assert isids[2502201]["cvids"] == {2201}
    assert isids[2502201]["name"] == "quarantaine"


def test_parse_isid_local_unknown_type_and_vlanid_column():
    # future/unknown TYPE values must still anchor a new I-SID (never leak
    # endpoints into the previous one), and the VLANID column of newer
    # releases must be harvested; names starting with 'c' are real names
    output = (
        "10100    ELAN       100    c100:1/10           CONFIG   Server-100\n"
        "99999    FANCYNEW   555    -                   CONFIG   cvlan-names-ok\n"
        "3 out of 3 Total Num of i-sids displayed\n"
    )
    isids = voss_parsers.parse_isid_local(output)
    assert set(isids) == {10100, 99999}
    assert isids[10100]["cvids"] == {100}
    assert isids[99999]["cvids"] == {555}
    assert isids[99999]["name"] == "cvlan-names-ok"


def test_parse_isis_spbm_isid(fixture):
    rows = voss_parsers.parse_isis_spbm_isid(
        fixture("voss", "show_isis_spbm_i_sid_all.txt"))
    assert len(rows) == 5
    assert {r["isid"] for r in rows} == {10100, 10200, 20300, 20400}
    assert rows[0] == {"isid": 10100, "type": "config", "host": "dvr-01"}
    assert rows[1] == {"isid": 10100, "type": "discover", "host": "beb-07"}


def test_parse_dvr_interfaces(fixture):
    rows = voss_parsers.parse_dvr_interfaces(
        fixture("voss", "show_dvr_interfaces.txt"))
    assert rows == [
        {"l2isid": 10100, "vlan": 100},
        {"l2isid": 10200, "vlan": 200},
        {"l2isid": 1501050, "vlan": 1050},  # L3VSN row: L3ISID 55501, VRF 5
    ]


def test_parse_lldp_neighbors_voss(fixture):
    neighbors = parse_lldp_neighbors(fixture("voss", "show_lldp_neighbor.txt"))
    assert neighbors == {"1/1": "core-01", "1/47": "old-agg-02"}


def test_parse_mlt_in_datapath(fixture):
    # the data-path table (LOCAL / LOCAL & REMOTE) is parsed onto each MLT so we
    # have a liveness signal even when no per-port state can be read
    mlts = {m.mlt_id: m for m in
            voss_parsers.parse_mlt(fixture("voss", "show_mlt.txt"))}
    assert mlts[1].in_datapath is True    # LOCAL & REMOTE
    assert mlts[10].in_datapath is True   # LOCAL


def test_parse_mlt_datapath_helper_reads_only_its_own_table():
    out = (
        "MLTID NAME   CREATED    PORT MEMBERS   PORT MEMBERS   IN DATA PATH\n"
        "-------------------------------------------------------------------\n"
        "1    a  LOC & REM  1/1  1/1  LOCAL\n"
        "2    b  LOC ONLY   1/2  -    NONE\n"
        "3    c  LOC & REM  1/3  1/3  LOCAL & REMOTE\n"
        "\nAll 3 out of 3 Total Num of mlt displayed\n"
        # a following table whose last column is up/down must NOT be misread
        "MLTID IFINDEX PORTS ADMIN OPER\n"
        "1  6144  1/1  enable  up\n"
    )
    assert voss_parsers._parse_mlt_datapath(out) == {1: True, 2: False, 3: True}


def test_parse_vlan_members(fixture):
    m = voss_parsers.parse_vlan_members(fixture("voss", "show_vlan_members.txt"))
    assert m[1] == []                                   # NONE -> no ports
    assert m[100] == ["1/1", "1/2", "2/1/1"]            # PORT MEMBER col, channelized
    assert m[200] == ["1/2", "1/47", "1/48"]
    assert m[4000] == ["1/47", "1/48"]
    # the 'N out of N Total' footer must not become a phantom VLAN
    assert 5 not in m or m[5] != []
