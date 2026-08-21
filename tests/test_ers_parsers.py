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
    # Port Members continuation line is attached to each VLAN
    assert by_id[1].members == []                        # NONE
    assert by_id[100].members == ["1", "2", "3", "4", "5", "6", "7", "8", "10", "26"]
    assert by_id[200].members == ["11", "12", "13", "49", "50"]


def test_parse_mlt(fixture):
    mlts = ers_parsers.parse_mlt(fixture("ers", "show_mlt.txt"))
    # MLT 3 ('Trunk #3' Disabled + NONE) is an unconfigured slot: dropped
    assert [m.mlt_id for m in mlts] == [1, 2, 6]
    uplink = mlts[0]
    assert uplink.name == "UPLINK MLT"
    assert uplink.members == ["49", "50"]
    assert uplink.admin == "Enabled"
    dead = mlts[1]     # Enabled but memberless: genuinely dead, kept
    assert dead.members == []
    assert dead.name == "old-leftover"
    ist = mlts[2]
    assert ist.is_ist
    assert ist.members == ["47", "48"]


def test_parse_mlt_59xx_lacp_key_format():
    # 59xx BOSS appends a KEY column and leaves TYPE empty on unconfigured
    # slots. The active LACP trunk row ('... Enabled Trunk NONE') used to be
    # DROPPED because tokens[-2] was 'Trunk', while all 64 empty 'Trunk #N'
    # slots parsed and would flood the report as dead MLTs.
    output = (
        "                                                                         LACP\n"
        "Id  Name             Members                Bpdu   Mode  Status   Type   Key\n"
        "--- ---------------- ---------------------- ------ ----- -------- ------ ----\n"
        "1   uplink.a         49-50                  All    A     Enabled  Trunk  NONE\n"
        "2   Trunk #2         NONE                   All    B     Disabled        NONE\n"
        "3   Trunk #3         NONE                   All    B     Disabled        NONE\n"
        "64  Trunk #64        NONE                   All    B     Disabled        NONE\n"
        "MODE Legend:\n"
        "B=Basic, A=Advance, E=Enhanced, Man=ManLag, Dyn=DynLag\n"
    )
    mlts = ers_parsers.parse_mlt(output)
    assert [m.mlt_id for m in mlts] == [1]
    assert mlts[0].name == "uplink.a"
    assert mlts[0].members == ["49", "50"]
    assert mlts[0].mlt_type == "Trunk"        # the KEY column must not shadow it
    assert mlts[0].admin == "Enabled"


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
    assert {p: n.sysname for p, n in neighbors.items()} == {
        "49": "dvr-01", "50": "dvr-02"}


def test_expand_port_list():
    assert expand_port_list("1/1-1/3,1/10") == ["1/1", "1/2", "1/3", "1/10"]
    assert expand_port_list("49-50") == ["49", "50"]
    assert expand_port_list("NONE") == []
    assert expand_port_list("2/1/1") == ["2/1/1"]


def test_parse_mlt_digit_shaped_name_is_not_read_as_members():
    # a trunk literally named '7' must not have its NAME column mistaken for
    # the members column (STATUS at index 4 would put members three back = name)
    out = ("Id Name Members Bpdu Mode Status Type\n"
           "-- ---- ------- ---- ---- ------ ----\n"
           "4  7    NONE    All  Basic Enabled Trunk\n")
    mlts = ers_parsers.parse_mlt(out)
    assert len(mlts) == 1
    assert mlts[0].mlt_id == 4
    assert mlts[0].name == "7"
    assert mlts[0].members == []


def test_wrapped_port_members_line_keeps_every_port():
    """A long 'Port Members:' list wraps onto an unlabelled continuation line.

    The trailing comma used to make expand_port_list reject the whole string,
    so the VLAN came back with no ports at all - not even the ones before the
    break.
    """
    out = """\
100  Users Floor 1        Port     None             0x0000 Yes    IVL     No
        Port Members: 1-8,10,26,
                      30-32,41
200  Printers             Port     None             0x0000 Yes    IVL     No
        Port Members: 11-13
"""
    vlans = {v.vlan_id: v.members for v in ers_parsers.parse_vlans(out)}
    assert vlans[100] == ["1", "2", "3", "4", "5", "6", "7", "8", "10", "26",
                          "30", "31", "32", "41"]
    assert vlans[200] == ["11", "12", "13"]


def test_unwrapped_member_list_is_not_extended_by_the_next_line():
    out = """\
100  Users                Port     None             0x0000 Yes    IVL     No
        Port Members: 1-3
99   99                   Port     None             0x0000 Yes    IVL     No
        Port Members: 7
"""
    vlans = {v.vlan_id: v.members for v in ers_parsers.parse_vlans(out)}
    assert vlans[100] == ["1", "2", "3"]
    assert vlans[99] == ["7"]
