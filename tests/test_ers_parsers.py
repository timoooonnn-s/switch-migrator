from switch_migrator.parsers import ers_parsers
from switch_migrator.parsers.common import expand_port_list, parse_lldp_neighbors


def test_parse_ports(fixture):
    ports = ers_parsers.parse_ports(fixture("ers", "show_interfaces.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1"].admin_up is True and by_port["1"].oper_up is True
    assert by_port["2"].oper_up is False
    assert by_port["3"].admin_up is False
    assert by_port["49"].oper_up is True


def test_parse_vlans(fixture):
    vlans = ers_parsers.parse_vlans(fixture("ers", "show_vlan.txt"))
    by_id = {v.vlan_id: v for v in vlans}
    assert set(by_id) == {1, 100, 200, 300, 666}
    assert by_id[100].name == "Users Floor 1"
    assert by_id[1].name == "VLAN #1"


def test_parse_mlt(fixture):
    mlts = ers_parsers.parse_mlt(fixture("ers", "show_mlt.txt"))
    assert len(mlts) == 3
    uplink = mlts[0]
    assert uplink.mlt_id == 1
    assert uplink.name == "UPLINK MLT"
    assert uplink.members == ["49", "50"]
    assert uplink.admin == "Enabled"
    empty = mlts[1]
    assert empty.members == []
    assert empty.name == ""
    ist = mlts[2]
    assert ist.is_ist
    assert ist.members == ["47", "48"]


def test_parse_ist(fixture):
    ist = ers_parsers.parse_ist(fixture("ers", "show_ist.txt"))
    assert ist is not None
    assert ist.enabled is True
    assert ist.peer_ip == "192.168.255.1"
    assert ist.vlan == 4000
    assert ist.session_up is True


def test_parse_ist_absent():
    assert ers_parsers.parse_ist("some unrelated output") is None


def test_parse_lldp_neighbors_ers(fixture):
    neighbors = parse_lldp_neighbors(fixture("ers", "show_lldp_neighbor.txt"))
    assert neighbors == {"49": "bcb-01", "50": "bcb-02"}


def test_expand_port_list():
    assert expand_port_list("1/1-1/3,1/10") == ["1/1", "1/2", "1/3", "1/10"]
    assert expand_port_list("49-50") == ["49", "50"]
    assert expand_port_list("NONE") == []
    assert expand_port_list("2/1/1") == ["2/1/1"]
