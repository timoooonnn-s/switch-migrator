"""Output shapes from releases we have no capture of yet.

VOSS/Fabric Engine 9.3-9.4 and ERS/BOSS 8.x are all in the estate. The
fixtures behind these tests are DOC-DERIVED, not captured from a device (see
the README beside each), so they pin one thing only: that a shape the parsers
have not met before degrades into less data rather than into no data.

Every change these guard is tolerance-only - nothing here narrows what an 8.x
box already parses correctly.
"""

from pathlib import Path

from switch_migrator.parsers import ers_parsers, voss_parsers
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
    assert "1/16" in members[1] and "2/1" in members[1]
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
