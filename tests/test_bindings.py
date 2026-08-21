"""VLAN<->I-SID bindings: the pairs, their tagging, and where they came from.

These pin the four defects that made cabling sheets come back incomplete, and
the running-config source that makes tagged-vs-untagged knowable at all.
"""

from pathlib import Path

from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch, resolve_binding_isids
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import (
    BOTH,
    TAGGED,
    UNTAGGED,
    FabricState,
    Platform,
    VlanBinding,
    tagging_summary,
)
from switch_migrator.parsers import ers_config, voss_config
from switch_migrator.report.migration import _pairs_cell

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(**kw) -> Config:
    base = dict(dvr_controllers=[], core_switch_patterns=["core-*", "dvr-*"],
                isid_offsets=[2500000], isid_explicit={}, excluded_vlans={1},
                ssh=SshSettings())
    base.update(kw)
    return Config(**base)


def _collect(platform: str):
    target = SwitchTarget(platform, platform, Platform(platform))
    return collect_switch(target, OfflineRunner(platform, FIXTURES), _cfg(),
                          pull_config=True, pull_macs=True)


def _with_fabric(audit):
    """Resolve against a fabric that attaches every VLAN at offset 2500000."""
    fabric = FabricState()
    for vlan in audit.vlans:
        fabric.get_or_create(2500000 + vlan.vlan_id).cvids.add(vlan.vlan_id)
    fabric.dvrs_ok = ["dvr-01"]
    resolve_binding_isids([audit], {audit.name: compare_switch(audit, fabric, _cfg())})
    return audit


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_a_binding_renders_as_one_self_contained_pair():
    assert VlanBinding(200, 10200, TAGGED).render() == "200->10200 (t)"
    assert VlanBinding(100, 10100, UNTAGGED).render() == "100->10100 (u)"
    assert VlanBinding(695, 2500695, BOTH).render() == "695->2500695 (t+u)"


def test_an_unresolved_isid_says_so_instead_of_going_blank():
    """A blank cell reads as 'no I-SID here'. These mean something else."""
    assert VlanBinding(4000, None, isid_note="local").render() == "4000->(local)"
    assert VlanBinding(99, None, isid_note="excluded").render() == "99->(excluded)"
    assert VlanBinding(2200, None).render() == "2200->?"


def test_untagged_traffic_without_a_cvid_is_still_a_row():
    assert VlanBinding(None, 2500695, UNTAGGED).render() == "untagged->2500695 (u)"


def test_pairs_are_ordered_untagged_first_then_by_vlan():
    cell = _pairs_cell([VlanBinding(200, None, ""),
                        VlanBinding(174, None, TAGGED),
                        VlanBinding(735, None, UNTAGGED),
                        VlanBinding(100, 10100, TAGGED)])
    assert cell == "735->? (u), 100->10100 (t), 174->? (t), 200->?"


def test_tagging_summary_only_speaks_when_the_bindings_do():
    assert tagging_summary([]) == ""
    assert tagging_summary([VlanBinding(1, None, "")]) == ""
    assert tagging_summary([VlanBinding(1, None, TAGGED)]) == "tagged"
    assert tagging_summary([VlanBinding(1, None, UNTAGGED)]) == "untagged"
    assert tagging_summary([VlanBinding(1, None, TAGGED),
                            VlanBinding(2, None, UNTAGGED)]) == "mixed"


def test_a_single_tagged_vlan_is_not_called_untagged():
    """The rule this replaced counted VLANs: one meant untagged, which turned
    a trunk carrying one tagged VLAN into a wrong port on the new switch."""
    assert tagging_summary([VlanBinding(695, 2500695, TAGGED)]) == "tagged"


# --------------------------------------------------------------------------- #
# running-config as the source of tagging
# --------------------------------------------------------------------------- #

def test_voss_flex_uni_cvid_and_untagged_traffic_are_read():
    model = voss_config.parse_voss_config(
        (FIXTURES / "voss" / "show_running_config.txt").read_text())
    bindings = voss_config.port_bindings(model)
    assert [b.render() for b in bindings["1/4"]] == [
        "695->2500695 (t+u)",     # a c-vid that also takes the untagged traffic
        "735->2510735 (t)",
    ]


def test_voss_traditional_membership_is_tagged_against_the_default_vlan():
    model = voss_config.parse_voss_config("""
vlan create 100 name "a" type port
vlan create 200 name "b" type port
vlan i-sid 100 10100
vlan i-sid 200 10200
vlan members add 100 1/1-1/2 portmember
vlan members add 200 1/2 portmember
interface GigabitEthernet 1/2
default-vlan-id 100
encapsulation dot1q
exit
""")
    bindings = voss_config.port_bindings(model)
    assert [b.render() for b in bindings["1/2"]] == ["100->10100 (u)",
                                                     "200->10200 (t)"]
    # 1/1 has no default-vlan-id and no dot1q - nothing in the config says how
    # it egresses, so nothing is claimed
    assert bindings["1/1"][0].tagging == ""


def test_voss_vlan_members_remove_is_honoured():
    model = voss_config.parse_voss_config("""
vlan members add 100 1/1-1/4 portmember
vlan members remove 100 1/2-1/3 portmember
""")
    assert model.vlan_members[100] == ["1/1", "1/4"]


def test_ers_tagging_comes_from_the_tagging_and_pvid_lines():
    model = ers_config.parse_ers_config(
        (FIXTURES / "ers" / "show_running_config.txt").read_text())
    bindings = ers_config.port_bindings(model)
    # port 49 is tagAll: everything egresses tagged
    assert {b.tagging for b in bindings["49"]} == {TAGGED}
    # port 7 is untagAll with pvid 695
    assert [b.render() for b in bindings["7"]] == ["695->? (u)"]
    # port 10 is tagAll and a member of one VLAN - still tagged, not untagged
    assert [b.render() for b in bindings["10"]] == ["174->? (t)"]


# --------------------------------------------------------------------------- #
# The four defects, end to end
# --------------------------------------------------------------------------- #

def test_ers_ports_and_mlts_get_isids_from_the_fabric():
    """D2: ERS has no local VLAN<->I-SID binding, so without the comparison
    every ERS I-SID cell on the sheet was empty."""
    audit = _with_fabric(_collect("ers"))
    port = {p.port: p for p in audit.ports}["49"]
    assert port.isids, "an ERS port reached the sheet with no I-SID"
    assert all(b.isid is not None for b in port.bindings if b.vlan != 1)
    uplink = {m.mlt_id: m for m in audit.mlts}[1]
    assert uplink.isids


def test_ers_mlt_vlans_are_derived_from_its_members():
    """D3: ERS 'show mlt' has no VLAN column, so MLT VLANs were always empty."""
    audit = _collect("ers")
    uplink = {m.mlt_id: m for m in audit.mlts}[1]
    assert uplink.members == ["49", "50"]
    members = {p.port: p for p in audit.ports}
    assert set(uplink.vlans) == set(members["49"].vlans) | set(members["50"].vlans)
    assert uplink.vlans


def test_no_vlan_is_dropped_for_want_of_an_isid():
    """D4: the I-SID list used to be shorter than the VLAN list, silently."""
    audit = _collect("voss")
    for holder in [*audit.ports, *audit.mlts]:
        rendered = _pairs_cell(holder.bindings)
        for vlan in holder.vlans:
            assert f"{vlan}->" in rendered, f"VLAN {vlan} vanished from the cell"


def test_ers_tagging_survives_into_the_collected_ports():
    audit = _collect("ers")
    by_port = {p.port: p for p in audit.ports}
    assert by_port["49"].tagging == "tagged"      # tagAll uplink
    assert by_port["1"].tagging == "untagged"     # access port


def test_config_only_vlans_reach_the_comparison():
    """A VLAN the running-config configures but 'show vlan' never listed still
    exists on the box - and has to be resolved, not left as a '?'."""
    audit = _with_fabric(_collect("ers"))
    # 2950 is in the ERS running-config fixture, not in its 'show vlan' output
    assert 2950 in {v.vlan_id for v in audit.vlans}
    uplink = {p.port: p for p in audit.ports}["49"]
    binding = next(b for b in uplink.bindings if b.vlan == 2950)
    assert binding.isid == 2502950


# --------------------------------------------------------------------------- #
# Provenance and coverage
# --------------------------------------------------------------------------- #

def test_a_binding_records_every_source_that_contributed():
    audit = _collect("voss")
    by_port = {p.port: p for p in audit.ports}
    assert "vlan-members" in by_port["1/1"].binding_sources
    assert "port-i-sid" in by_port["1/1"].binding_sources


def test_the_running_config_is_recorded_as_a_source():
    audit = _collect("ers")
    named = {s.name: s for s in audit.sources}
    assert named["running-config"].ok


def test_coverage_flags_a_switch_whose_ports_have_no_vlans():
    from switch_migrator.models import PortState, SwitchAudit
    from switch_migrator.report.tables import build_coverage

    thin = SwitchAudit(name="thin-01", host="h", platform=Platform.VOSS,
                       reachable=True)
    thin.ports = [PortState(port=f"1/{n}") for n in range(1, 11)]
    thin.record_source("vlan membership", False, "no VLAN carried member ports")
    table = build_coverage([thin])
    row = dict(zip(table.headers, table.rows[0]))
    assert row["Ports with VLANs"] == 0
    assert row["VLAN coverage"] == "0%"
    assert "vlan membership" in row["Sources that did not"]
    assert table.severities[0] == "error"


def test_coverage_is_clean_when_every_port_has_vlans():
    audit = _collect("ers")
    from switch_migrator.report.tables import build_coverage
    row = dict(zip(build_coverage([audit]).headers,
                   build_coverage([audit]).rows[0]))
    assert row["VLAN coverage"] == "100%"
    assert row["Ports with tagging"] == len(audit.ports)


def test_no_fabric_mode_does_not_claim_a_failed_lookup():
    """--no-fabric is for estates with no fabric by design. Rendering '?' there
    would flag every VLAN on the sheet for a lookup that never happened."""
    audit = _collect("ers")
    resolve_binding_isids([audit], {}, fabric_checked=False)
    cell = _pairs_cell({p.port: p for p in audit.ports}["49"].bindings)
    assert "?" not in cell
    assert "(no fabric)" in cell


def test_an_unresolved_isid_still_says_so_when_the_fabric_was_read():
    audit = _collect("ers")
    fabric = FabricState()
    fabric.dvrs_ok = ["dvr-01"]          # read, but it knows nothing
    resolve_binding_isids([audit], {audit.name: compare_switch(audit, fabric, _cfg())})
    cell = _pairs_cell({p.port: p for p in audit.ports}["49"].bindings)
    assert "?" in cell


def test_the_raw_config_is_dropped_once_its_tagging_is_extracted():
    """The config is read for tagging on every migration-sheets run, but it
    holds RADIUS keys and SNMP users - and the snapshot is a file meant to be
    handed to a colleague. Only --extract-config, which exists to neutralize
    it, keeps the original text."""
    target = SwitchTarget("ers", "ers", Platform.ERS)
    audit = collect_switch(target, OfflineRunner("ers", FIXTURES), _cfg(),
                           pull_config=True, pull_macs=True, keep_config=False)
    assert audit.running_config == ""
    # ...and the tagging it was read for survived
    assert {p.port: p for p in audit.ports}["49"].tagging == "tagged"


def test_keeping_the_config_is_still_possible_for_the_extract():
    audit = _collect("ers")           # collect_switch(..., keep_config default)
    assert "radius server host" in audit.running_config
