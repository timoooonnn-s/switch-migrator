"""Output shapes from releases we have no capture of yet.

VOSS/Fabric Engine 9.3-9.4 and ERS/BOSS 8.x are all in the estate. The
fixtures behind these tests are DOC-DERIVED, not captured from a device (see
the README beside each), so they pin one thing only: that a shape the parsers
have not met before degrades into less data rather than into no data.

Every change these guard is tolerance-only - nothing here narrows what an 8.x
box already parses correctly.
"""

from pathlib import Path

from switch_migrator.parsers import common, ers_parsers, voss_parsers
from switch_migrator.parsers.common import expand_port_list

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# VOSS 9.x: channelized (breakout) sub-port ranges
# --------------------------------------------------------------------------- #

def test_channelized_range_across_parent_ports_is_enumerated():
    """'1/17/1-1/18/4' is how VOSS prints a breakout range. Keeping only the
    two endpoints lost the six ports in between - and with them their VLAN."""
    assert expand_port_list("1/17/1-1/18/4", span_subports=True) == [
        "1/17/1", "1/17/2", "1/17/3", "1/17/4",
        "1/18/1", "1/18/2", "1/18/3", "1/18/4"]


def test_channelized_range_starting_mid_parent():
    assert expand_port_list("1/17/3-1/18/4", span_subports=True) == [
        "1/17/3", "1/17/4", "1/18/1", "1/18/2", "1/18/3", "1/18/4"]


def test_mlt_members_never_gain_an_invented_sub_port():
    """The enumeration guesses the channelization width, so it is confined to
    VLAN membership, where an invented port matches nothing and is dropped. On
    an MLT it would look like a down leg and fake a degraded aggregation - and
    a degraded MLT is a health-check BLOCK."""
    assert expand_port_list("1/17/1-1/18/4") == ["1/17/1", "1/18/4"]


def test_a_cross_slot_range_is_still_left_alone():
    """Different slots say nothing about how many ports the first one has."""
    assert expand_port_list("1/47-2/2", span_subports=True) == ["1/47", "2/2"]


def test_flat_and_same_parent_ranges_are_unaffected():
    assert expand_port_list("1/1-1/4", span_subports=True) == [
        "1/1", "1/2", "1/3", "1/4"]
    assert expand_port_list("1/17/1-1/17/4", span_subports=True) == [
        "1/17/1", "1/17/2", "1/17/3", "1/17/4"]


def test_voss_9x_vlan_members_with_channelized_and_wrapped_lists():
    """Both 9.x shapes at once: a wrapped PORT MEMBER list whose continuation
    also carries a channelized range."""
    out = (FIXTURES / "voss_9x" / "show_vlan_members.txt").read_text()
    members = voss_parsers.parse_vlan_members(out)

    # VLAN 100 is a plain channelized range
    assert members[100] == ["1/17/1", "1/17/2", "1/17/3", "1/17/4",
                            "1/18/1", "1/18/2", "1/18/3", "1/18/4"]
    # VLAN 1 wraps mid-range: the break lands inside '1/17/1-1/18/4'
    assert "1/16" in members[1]
    assert "2/1" in members[1]
    assert members[1].count("1/17/2") == 1
    assert len(members[1]) == 16 + 8 + 1
    # and the row after the wrap is not swallowed by it
    assert members[200] == ["1/1", "1/2"]


# --------------------------------------------------------------------------- #
# ERS / BOSS 8.x: a VLAN TYPE this build has never seen
# --------------------------------------------------------------------------- #

def test_an_unknown_vlan_type_does_not_drop_the_vlan():
    """The type keyword anchored the name column, so a release adding a type
    made the whole VLAN vanish from the report - silently."""
    out = (FIXTURES / "ers_boss8x" / "show_vlan.txt").read_text()
    vlans = {v.vlan_id: v for v in ers_parsers.parse_vlans(out)}

    assert set(vlans) == {1, 100, 735}
    assert vlans[735].name == "Voice VLAN"       # type 'Voice' is not known
    assert vlans[735].members == ["1", "2", "3", "4", "5", "6", "7", "8"]
    # the known-type rows still parse exactly as before
    assert vlans[100].members == ["1", "2", "3", "4", "5", "6", "7", "8",
                                  "10", "26", "30", "31", "32", "41"]
    assert vlans[1].members == []


def test_the_fallback_does_not_invent_vlans_from_other_output():
    """It is anchored on the PID column, so lines from other tables and the
    footers cannot become VLANs."""
    assert ers_parsers.parse_vlans("Total VLANs: 5\n") == []
    assert ers_parsers.parse_vlans("Number of addresses: 1655\n") == []
    assert ers_parsers.parse_vlans(
        "0011.2200.0011       735 Dynamic Port:7\n") == []


def test_a_break_inside_a_range_does_not_read_the_next_column():
    """The worst failure of the pair. '1/1-1/16,1/17/1-' was not recognised as
    a port list at all, so the column scan fell through to the ACTIVE MEMBER
    column and reported THAT as the port members - wrong data, not missing
    data, and nothing said so."""
    out = """\
VLAN     PORT                  ACTIVE                STATIC
ID       MEMBER                MEMBER                MEMBER
--------------------------------------------------------------------------------
1        1/1-1/4,1/17/1-       1/1-1/2               1/1-1/4
         1/18/4
"""
    members = voss_parsers.parse_vlan_members(out)
    assert members[1] == ["1/1", "1/2", "1/3", "1/4",
                          "1/17/1", "1/17/2", "1/17/3", "1/17/4",
                          "1/18/1", "1/18/2", "1/18/3", "1/18/4"]


def test_an_open_range_on_its_own_reads_as_its_start():
    assert expand_port_list("1/17/1-") == ["1/17/1"]
    assert expand_port_list("1/1-1/4,1/9-") == ["1/1", "1/2", "1/3", "1/4", "1/9"]


# --------------------------------------------------------------------------- #
# VOSS / Fabric Engine 9.4: columns 8.x does not print
#
# These use REAL 9.4 captures (fixtures/voss_94, see its README). Each one
# pinned a parser that read a 9.x column as an 8.x one and produced a WRONG
# value rather than a missing one - the failure mode the whole parser package
# is written to avoid.
# --------------------------------------------------------------------------- #

VOSS_94 = FIXTURES / "voss_94"


def _capture(name: str) -> str:
    return (VOSS_94 / name).read_text()


def test_9x_vlan_isid_keeps_the_vlan_name_out_of_the_isid_name():
    """9.x adds a VLAN NAME column between VLAN_ID and I-SID NAME. Read as 8.x,
    the two names came back glued into one cell - and that cell is what the
    sheets and the fabric state use as the I-SID's name."""
    vlans = {v.vlan_id: v for v in voss_parsers.parse_vlan_isid(
        _capture("show_vlan_i_sid.txt"))}

    assert vlans[40].isid == 20040
    assert vlans[40].isid_name == "ISID-20040"
    assert vlans[40].name == "servers"          # free: no 'show vlan basic' needed
    # a multi-word I-SID name survives whole
    assert vlans[4048].isid_name == "Onboarding I-SID"
    assert vlans[4048].name == "onboarding-vlan"
    # a VLAN with no I-SID is still a VLAN, and gains no name it does not have
    assert vlans[4051].isid is None
    assert vlans[4051].isid_name == ""
    assert set(vlans) == {1, 40, 50, 60, 70, 71, 72, 73, 74, 75, 76, 77, 78,
                          4000, 4048, 4051, 4052}


def test_9x_lldp_summary_keeps_the_first_character_of_sysdescr():
    """9.x prints SYSDESCR one position LEFT of its header word. Cutting at the
    header offset ate the leading character, so a 5520 was reported as a
    '520-24T-FabricEngine' - a model that does not exist."""
    neighbors = common.parse_lldp_neighbors_summary(
        _capture("show_lldp_neighbor_summary.txt"))

    assert neighbors["1/1"].sys_descr == "5520-24T-FabricEngine (9.4.0.0)"
    assert neighbors["1/1"].sysname == "fe-02"
    assert neighbors["1/1"].ip == "10.99.0.12"
    # the block form of the same output has always been right: they must agree
    blocks = common.parse_lldp_neighbors(_capture("show_lldp_neighbor.txt"))
    for port, summary in neighbors.items():
        assert summary.sys_descr == blocks[port].sys_descr


def test_9x_spbm_isid_host_is_the_bebs_name_not_its_area():
    """9.x appends ISID NAME / AREA / AREA NAME after HOST_NAME. Taking the last
    token made every announcing host 'HOME', so the fabric report named an area
    instead of the BEBs that advertise each service."""
    rows = voss_parsers.parse_isis_spbm_isid(_capture("show_isis_spbm_i_sid_all.txt"))

    hosts = {row["host"] for row in rows}
    assert "HOME" not in hosts
    assert hosts == {"fe-01", "fe-02", "fe-03", "fe-04"}
    assert {"isid": 20040, "type": "config", "host": "fe-01"} in rows
    assert {"isid": 20040, "type": "discover", "host": "fe-02"} in rows


def test_a_long_vlan_name_does_not_spill_into_the_isid_name():
    """VLAN NAME is 33 wide on 9.x but a VLAN name may be 64. Cut at the
    header, the tail of the VLAN name became the start of the I-SID name."""
    text = _capture("show_vlan_i_sid.txt").replace(
        "40         20040                servers                          ISID-20040",
        "40         20040                dc1-servers-production-backend-vlan ISID-20040")
    vlans = {v.vlan_id: v for v in voss_parsers.parse_vlan_isid(text)}

    assert vlans[40].name == "dc1-servers-production-backend-vlan"
    assert vlans[40].isid_name == "ISID-20040"
    assert vlans[4048].isid_name == "Onboarding I-SID"


def test_a_long_host_name_is_not_cut_at_the_isid_name_column():
    """HOST_NAME leaves 31 characters on 9.x. A longer sysname was cut short,
    naming a BEB that does not exist."""
    text = _capture("show_isis_spbm_i_sid_all.txt").replace(
        "config       fe-01                          ISID-20040",
        "config       gx-11-s72-p1-core-distribution-01 ISID-20040")
    rows = voss_parsers.parse_isis_spbm_isid(text)

    assert {"isid": 20040, "type": "config",
            "host": "gx-11-s72-p1-core-distribution-01"} in rows
    assert {"isid": 20060, "type": "config", "host": "fe-01"} in rows


def test_a_long_port_list_does_not_push_origin_into_the_isid_name():
    """A port list longer than its column pushes the next values right. Cut at
    the NAME offset, 8.x read 'CONFIG     Server-100' as the name, and 9.x
    read the ORIGIN flag slots into it."""
    eight = (FIXTURES / "voss" / "show_i_sid.txt").read_text().replace(
        "10100    ELAN       c100:1/10,c100:1/11 c100:2            CONFIG     Server-100",
        "10100    ELAN       c100:1/10,c100:1/11,c100:1/12,c100:1/13 c100:2 CONFIG Server-100")
    isids = voss_parsers.parse_isid_local(eight)
    assert isids[10100]["name"] == "Server-100"
    assert isids[10200]["name"] == "Server-200"

    nine = _capture("show_i_sid.txt").replace(
        "20040     CVLAN    40       1/4                -                  "
        "C  ---  -  ---  -  -  --    ISID-20040",
        "20040     CVLAN    40       1/4,1/5,1/6,1/7,1/8,1/9,1/10 -           "
        "C  ---  -  ---  -  -  -- ISID-20040")
    isids = voss_parsers.parse_isid_local(nine)
    assert isids[20040]["name"] == "ISID-20040"
    assert isids[15999999]["name"] == "Onboarding I-SID"


def test_isid_names_with_spaces_survive():
    """The name was the last TOKEN of the row, so 'Onboarding I-SID' arrived as
    'I-SID'. True on 8.x as well; 9.x only makes it easier to hit."""
    isids = voss_parsers.parse_isid_local(_capture("show_i_sid.txt"))

    assert isids[15999999]["name"] == "Onboarding I-SID"
    assert isids[15999999]["cvids"] == {4048}
    assert isids[20040]["name"] == "ISID-20040"
    assert isids[30100]["cvids"] == {100}       # from the 'c100:' binding marker


def test_an_mlt_with_no_members_keeps_its_vlans():
    """The VLAN IDS list was read as 'whatever follows PORT MEMBERS', so an MLT
    with an empty members column lost its VLANs entirely. An LACP MLT whose
    ports are configured but not up is exactly that shape - and it is not a
    dead MLT, it is one that has to be recreated carrying VLAN 50."""
    mlts = {m.mlt_id: m for m in voss_parsers.parse_mlt(
        _capture("show_mlt_members_not_up.txt"))}

    assert mlts[10].members == []
    assert mlts[10].vlans == [50]
    assert mlts[10].in_datapath is False
    assert voss_parsers.parse_mlt_lacp(_capture("show_mlt_members_not_up.txt")) == {10: True}


def test_9x_mlt_with_members_is_unchanged():
    """The ORIGIN column 9.x inserts before VLAN IDS must not become a VLAN."""
    mlts = {m.mlt_id: m for m in voss_parsers.parse_mlt(_capture("show_mlt.txt"))}

    assert mlts[10].members == ["1/6", "1/7"]
    assert mlts[10].vlans == [50]
    assert mlts[10].mlt_type == "trunk"
    assert mlts[20].admin == "smlt"
    assert mlts[20].members == ["1/5"]
    assert mlts[20].vlans == [60]


def test_9x_port_and_vlan_tables_still_parse():
    """The tables that did NOT change shape, so a 9.x capture is covered end to
    end rather than only where a bug was found."""
    states = {p.port: p for p in voss_parsers.parse_port_state(
        _capture("show_interfaces_gigabitethernet_state.txt"))}
    assert states["1/1"].oper_up is True
    assert states["1/1"].last_change == "11/22/26 18:19:03"
    assert states["1/1"].state_reason == ""     # no REASON: not the DATE column

    ports = {p.port: p for p in voss_parsers.parse_ports(
        _capture("show_interfaces_gigabitethernet_interface.txt"))}
    assert ports["1/1"].media == "1000BaseTX"

    members = voss_parsers.parse_vlan_members(_capture("show_vlan_members.txt"))
    assert members[50] == ["1/6", "1/7"]
    assert voss_parsers.parse_vlan_basic(_capture("show_vlan_basic.txt"))[40] == "servers"

    rows = voss_parsers.parse_port_isid(
        _capture("show_interfaces_gigabitethernet_i_sid.txt"))
    assert {"port": "1/4", "isid": 20040, "vlan": 40} in rows
    # 'untag' in the C-VID column is untagged traffic, which has no VLAN of its own
    assert {"port": "1/9", "isid": 30100, "vlan": None} in rows
