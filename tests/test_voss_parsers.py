from switch_migrator.parsers import voss_parsers
from switch_migrator.parsers.common import parse_lldp_neighbors


def test_parse_ports(fixture):
    ports = voss_parsers.parse_ports(
        fixture("voss", "show_interfaces_gigabitethernet_interface.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1/1"].admin_up is True and by_port["1/1"].oper_up is True
    assert by_port["1/2"].admin_up is True and by_port["1/2"].oper_up is False
    assert by_port["1/48"].admin_up is False
    assert by_port["2/1/1"].oper_up is True  # channelized port


def test_parse_mlt(fixture):
    mlts = voss_parsers.parse_mlt(fixture("voss", "show_mlt.txt"))
    assert len(mlts) == 3
    ist = mlts[0]
    assert ist.mlt_id == 1
    assert ist.is_ist
    assert ist.members == ["1/47", "1/48"]
    assert mlts[1].members == ["1/1", "1/2"]
    assert mlts[1].admin == "smlt"
    assert mlts[2].mlt_type == "access"
    assert mlts[2].members == ["2/1/1"]


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
    assert set(by_id) == {1, 100, 200, 300, 4000}
    assert by_id[100].isid == 10100
    assert by_id[100].name == "Server-VLAN-100"
    assert by_id[200].isid == 10200
    assert by_id[300].isid == 77777
    assert by_id[1].isid is None
    assert by_id[4000].isid is None


def test_parse_isid_local(fixture):
    isids = voss_parsers.parse_isid_local(fixture("voss", "show_i_sid.txt"))
    assert set(isids) == {10100, 10200, 20300}
    assert isids[10100]["cvids"] == {100}
    assert isids[10200]["cvids"] == {200}
    assert isids[20300]["cvids"] == {300}
    assert isids[10100]["name"] == "Server-100"


def test_parse_isis_spbm_isid(fixture):
    rows = voss_parsers.parse_isis_spbm_isid(
        fixture("voss", "show_isis_spbm_i_sid_all.txt"))
    assert len(rows) == 5
    assert {r["isid"] for r in rows} == {10100, 10200, 20300, 20400}
    assert rows[0] == {"isid": 10100, "type": "config", "host": "bcb-01"}
    assert rows[1] == {"isid": 10100, "type": "discover", "host": "beb-07"}


def test_parse_lldp_neighbors_voss(fixture):
    neighbors = parse_lldp_neighbors(fixture("voss", "show_lldp_neighbor.txt"))
    assert neighbors == {"1/1": "core-01", "1/47": "old-agg-02"}
