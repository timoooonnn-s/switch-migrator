"""ERS running-config parse + VOSS flex-UNI generation (slice 3)."""

from pathlib import Path

from switch_migrator.config import Config, SshSettings
from switch_migrator.config_generate import _compress, generate_voss_from_ers
from switch_migrator.parsers.ers_config import parse_ers_config

FIXTURES = Path(__file__).parent / "fixtures"


def _ers():
    return parse_ers_config((FIXTURES / "ers" / "show_running_config.txt").read_text())


def _cfg(**over) -> Config:
    base = dict(dvr_controllers=[], core_switch_patterns=[],
                isid_offsets=[2500000, 2510000], isid_explicit={},
                excluded_vlans=set(), excluded_vlan_names=["quarant*"],
                ssh=SshSettings())
    base.update(over)
    return Config(**base)


# ---- parser ----

def test_parse_ers_vlans_members_names():
    m = _ers()
    assert {99, 174, 695, 735, 2950} <= set(m.vlans)
    assert m.vlans[695].name == "servers"
    assert m.vlans[695].members == ["7", "8", "49", "50"]
    # 'vlan members 1 NONE' creates VLAN 1 with no ports (default VLAN emptied)
    assert m.vlans[1].members == []


def test_parse_ers_port_attributes():
    m = _ers()
    assert m.ports["49"].tagged and m.ports["10"].tagged
    assert not m.ports["7"].tagged
    assert m.ports["7"].pvid == 695 and m.ports["7"].name == "srv-esx-01"
    assert m.ports["2"].shutdown and m.ports["11"].shutdown
    assert m.mlts[0].mlt_id == 1 and m.mlts[0].members == ["49", "50"]


def test_compress_ranges():
    assert _compress([4, 5, 6, 9, 11, 12]) == "1/4-1/6,1/9,1/11-1/12"
    assert _compress([7, 8]) == "1/7-1/8"
    assert _compress([]) == ""


# ---- generator ----

def test_generate_maps_vlan_to_isid_service():
    m = _ers()
    # fabric confirmed 695 -> 2500695; 735 decided explicitly; 2950 undecided
    res = generate_voss_from_ers(
        m, _cfg(isid_explicit={735: 2510735}),
        matched_by_vlan={695: 2500695}, device_name="ers-access-01")
    text = res.text
    # 695: servers on access ports 7-8 (untagged, pvid 695) -> untagged-traffic,
    # uplink ports 49-50 excluded
    assert "i-sid 2500695 elan" in text
    assert "untagged-traffic port 1/7-1/8" in text
    assert "1/49" not in text and "1/50" not in text     # uplinks never UNI ports
    # 735 explicit
    assert "i-sid 2510735 elan" in text


def test_undecided_vlan_becomes_commented_review_placeholder():
    m = _ers()
    res = generate_voss_from_ers(m, _cfg(), device_name="ers-access-01")
    text = res.text
    # 2950 has no fabric/explicit -> REVIEW with candidates, block commented out
    assert "[REVIEW] VLAN 2950" in text
    assert "candidates 2502950 / 2512950" in text
    assert "# i-sid <ISID> elan" in text                 # placeholder, not a guess


def test_excluded_vlan_is_not_generated():
    m = _ers()
    res = generate_voss_from_ers(m, _cfg(), device_name="ers-access-01")
    # quarantine (99) excluded by name -> no service, and its access ports
    # (2,11-13,51-52) are not emitted solely for it
    assert "i-sid 2500099" not in res.text and "c-vid 99 " not in res.text


def test_high_sfp_ports_flagged_not_silently_dropped():
    m = _ers()
    # VLAN 99 excluded, but 2950 has no >48 access ports; craft one:
    m.vlans[735].members.append("51")   # ERS SFP port on an access VLAN
    m.ports["51"].pvid = 735
    res = generate_voss_from_ers(m, _cfg(isid_explicit={735: 2510735}))
    assert "ERS port(s) 51" in res.text and "no 1/N equivalent" in res.text


def test_flex_uni_access_port_blocks_generated():
    m = _ers()
    res = generate_voss_from_ers(m, _cfg(isid_explicit={735: 2510735, 174: 2510174,
                                                         2950: 2512950},
                                         excluded_vlan_names=["quarant*"]),
                                 matched_by_vlan={695: 2500695})
    text = res.text
    assert "interface GigabitEthernet 1/7" in text
    assert 'name "srv-esx-01"' in text
    assert "flex-uni enable" in text
    # shutdown state carried over
    assert "1/49" not in text                            # uplink not a flex-uni port


def test_cli_extract_config_ers_end_to_end(tmp_path):
    # --extract-config on an ERS box writes a generated .cfg AND an I-SID
    # decision worksheet, offline, no-fabric.
    import shutil
    from switch_migrator.cli import main
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "ers", raw / "ers-access-01")
    (tmp_path / "config.yaml").write_text(
        "isid_conventions:\n"
        "  offsets: [2500000, 2510000, 2700000, 2710000]\n"
        "excluded_vlan_names: [\"quarant*\"]\n")
    out = tmp_path / "out"
    rc = main([
        "-c", str(tmp_path / "config.yaml"), "--no-fabric", "--offline", str(raw),
        "-s", "ers-access-01:ers", "-o", str(out), "--no-excel", "--extract-config",
    ])
    assert rc in (0, 1)   # 0/1 = ran; the shared ERS fixture has a down MLT (not a crash)
    cfg = (out / "config" / "ers-access-01.cfg").read_text()
    assert "GENERATED VOSS flex-UNI config from ERS" in cfg
    assert "untagged-traffic port 1/7-1/8" in cfg          # 695 servers -> UNI
    assert "1/49" not in cfg                               # uplink excluded
    ws = (out / "config" / "ers-access-01.isid-decisions.txt").read_text()
    assert "I-SID decision worksheet" in ws
    assert "isid_conventions:" in ws                       # paste-ready snippet
    assert "VLAN 695" in ws                                # needs a decision (no fabric)


# ---- stacked ERS (unit-qualified port ids) --------------------------------

_STACKED = """\
vlan create 100 type port
vlan name 100 "servers"
vlan members add 100 1/5-1/6,2/5
vlan ports 1/5-1/6,2/5 pvid 100
name port 1/5 "srv-a"
vlan create 200 type port
vlan members add 200 2/49
mlt 1 name "uplink" enable member 1/49-1/50
"""


def test_stacked_ers_port_ids_do_not_crash_and_keep_their_unit():
    """A stack numbers its ports '<unit>/<port>'. Feeding those through the
    generator used to raise ValueError on int('1/1') and abort the whole run."""
    m = parse_ers_config(_STACKED)
    res = generate_voss_from_ers(m, _cfg(isid_explicit={100: 2510100}),
                                 device_name="ers-stack-01")
    text = res.text
    assert "untagged-traffic port 1/5-1/6,2/5" in text
    assert "interface GigabitEthernet 1/5" in text
    assert "interface GigabitEthernet 2/5" in text
    assert 'name "srv-a"' in text
    # 2/49 is above the copper range -> flagged for remapping, not emitted
    assert "2/49 (SFP/high)" in text
    assert "interface GigabitEthernet 2/49" not in text


def test_compress_never_bridges_a_range_across_units():
    assert _compress(["1/47", "1/48", "2/1", "2/2"]) == "1/47-1/48,2/1-2/2"
    assert _compress(["3"]) == "1/3"
    assert _compress(["ALL"]) == ""


def test_pvid_on_a_tagall_trunk_stays_a_cvid_not_untagged_traffic():
    """On an ERS trunk ('tagging tagAll') the PVID VLAN still egresses tagged,
    so the generator must keep it as c-vid - only a genuine access port
    becomes untagged-traffic."""
    m = parse_ers_config(
        "vlan create 100 type port\n"
        "vlan members 100 5,6\n"
        "vlan ports 5 tagging tagAll\n"
        "vlan ports 5 pvid 100\n"
        "vlan ports 6 pvid 100\n")
    res = generate_voss_from_ers(m, _cfg(isid_explicit={100: 2500100}))
    text = res.text
    assert "untagged-traffic port 1/6" in text          # true access port
    assert "untagged-traffic port 1/5" not in text      # trunk stays tagged
    assert "c-vid 100 port 1/5" in text
