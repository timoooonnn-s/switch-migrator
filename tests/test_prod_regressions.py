"""Regressions pinned to a REAL production VOSS capture (gx-11-s72-p1).

This is the switch dump from the field where:
  * every `show interfaces gigabitEthernet ...` and `show lldp neighbor ...`
    variant is rejected with '% Invalid input' (so there is NO port state), and
  * `show mlt` / `show vlan i-sid` / `show vlan basic` / `show virtual-ist`
    succeed.

The bugs these guard against:
  Issue 2 - VLAN 99 (quarantine) and other no-I-SID VLANs were flagged
            MISSING_ON_DVR (error) instead of being recognised as local-only.
  Issue 3 - populated MLTs were flagged 'only 0/N member ports up' purely
            because no port state could be read.
"""

from pathlib import Path

from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import CompStatus, FabricState, Platform

FIXTURES = Path(__file__).parent / "fixtures"
PROD = FIXTURES / "voss_prod"
DEVICE = "gx-11-s72-p1"


def _cfg() -> Config:
    return Config(
        dvr_controllers=[],
        core_switch_patterns=["dvr-*", "core-*"],
        isid_offsets=[2500000, 2510000],
        isid_explicit={},
        excluded_vlans=set(),
        ssh=SshSettings(),
    )


def _audit():
    target = SwitchTarget(DEVICE, DEVICE, Platform.VOSS)
    return collect_switch(target, OfflineRunner(DEVICE, PROD), _cfg())


def _fabric_from_switch(audit) -> FabricState:
    """A healthy fabric that confirms every I-SID the switch actually binds,
    so ONLY the no-I-SID VLANs can end up 'missing'."""
    fabric = FabricState()
    for v in audit.vlans:
        if v.isid is not None:
            fabric.get_or_create(v.isid).cvids.add(v.vlan_id)
    fabric.dvrs_ok = ["dvr-01"]
    return fabric


# --------------------------------------------------------------------------- #
# Issue 3: MLT member up-state with no port state available
# --------------------------------------------------------------------------- #

def test_prod_all_mlts_parsed_with_members_and_datapath():
    audit = _audit()
    by_id = {m.mlt_id: m for m in audit.mlts}
    assert set(by_id) == {1, 35, 38, 196, 197, 198, 199, 200}
    # members read fine from `show mlt`...
    assert by_id[1].members == ["1/8", "1/16"]
    assert by_id[35].members == ["1/1", "1/2", "1/23", "1/24"]
    # ...and every one is programmed in the data path (LOCAL / LOCAL & REMOTE)
    assert all(m.in_datapath is True for m in audit.mlts)


def test_prod_no_port_state_leaves_members_up_unknown():
    audit = _audit()
    # all interface variants were rejected -> no ports, and this IS reported
    assert audit.ports == []
    assert any("no port state obtained" in e for e in audit.errors)
    # members_up must be unknown (None), never a fabricated 0
    assert all(m.members_up is None for m in audit.mlts)


def test_prod_populated_mlts_not_flagged_as_zero_up():
    audit = _audit()
    bogus = [w for w in audit.warnings if "member ports up" in w]
    assert bogus == [], f"populated MLTs falsely flagged down: {bogus}"
    # and no data-path warnings either, since all MLTs are forwarding
    assert not [w for w in audit.warnings if "not programmed in the data path" in w]


def test_prod_datapath_none_is_flagged_when_port_state_missing(tmp_path):
    # take the real capture but force MLT 196 out of the data path
    import shutil
    dev = tmp_path / DEVICE
    shutil.copytree(PROD / DEVICE, dev)
    mlt = (dev / "show_mlt.txt").read_text().replace(
        "196  f110    LOC & REM  1/10              1/10              LOCAL & REMOTE",
        "196  f110    LOC & REM  1/10              1/10              NONE")
    (dev / "show_mlt.txt").write_text(mlt)

    target = SwitchTarget(DEVICE, DEVICE, Platform.VOSS)
    audit = collect_switch(target, OfflineRunner(DEVICE, tmp_path), _cfg())
    hits = [w for w in audit.warnings
            if "MLT 196" in w and "not programmed in the data path" in w]
    assert len(hits) == 1
    # the healthy MLTs are still silent
    assert not [w for w in audit.warnings if "MLT 35" in w]


# --------------------------------------------------------------------------- #
# Issue 2: local-only VLANs (no I-SID) must not be MISSING_ON_DVR errors
# --------------------------------------------------------------------------- #

def test_prod_quarantine_and_local_vlans_are_local_only():
    audit = _audit()
    res = {c.vlan_id: c for c in compare_switch(audit, _fabric_from_switch(audit), _cfg())}
    for vid in (1, 99, 4051, 4052):     # default, quarantine, two B-VLANs
        assert res[vid].status is CompStatus.LOCAL_ONLY, f"VLAN {vid}: {res[vid].status}"
        assert res[vid].severity == "ok"
    # nothing on this switch should be a false MISSING_ON_DVR
    assert not [c for c in res.values() if c.status is CompStatus.MISSING_ON_DVR]


def test_prod_bound_vlan_still_verified_against_fabric():
    audit = _audit()
    res = {c.vlan_id: c for c in compare_switch(audit, _fabric_from_switch(audit), _cfg())}
    # VLAN 31 binds I-SID 1531100 which the fabric confirms -> not local-only
    assert res[31].status is not CompStatus.LOCAL_ONLY
    assert res[31].matched_isid == 1531100


# --------------------------------------------------------------------------- #
# Privileged session (post-'enable') - the state the tool reaches now that it
# sends 'enable' after login. Fixtures are the REAL gx-11-s72-p1 outputs from
# a privileged interactive session (VSP-7254XSQ, VOSS 8.10.9.0).
# --------------------------------------------------------------------------- #

PRIV = "gx-11-s72-p1-priv"


def _cfg_priv() -> Config:
    cfg = _cfg()
    cfg.core_switch_patterns = ["gx-11-s74-*"]   # the new fabric BEBs
    return cfg


def _priv_audit():
    target = SwitchTarget(PRIV, PRIV, Platform.VOSS)
    return collect_switch(target, OfflineRunner(PRIV, PROD), _cfg_priv())


def test_priv_ports_come_from_interface_variant_fallback():
    # no `state` capture on purpose: the chain must fall back to
    # `show interfaces gigabitEthernet interface` and parse the real table
    audit = _priv_audit()
    assert len(audit.ports) == 54          # 48 + 6 x 40G
    assert audit.ports_up == 15
    assert not audit.errors
    by_port = {p.port: p for p in audit.ports}
    assert by_port["1/4"].admin_up is True and by_port["1/4"].oper_up is False
    assert by_port["2/6"].admin_up is False


def test_priv_lldp_summary_all_caps_header_and_server_rows():
    # real 8.10.9 header is ALL-CAPS 'SYSNAME' on the second header line, and
    # server neighbors have an EMPTY sysname cell - they must not pollute
    # neighbor names with 'ProLiant'/'HPE'
    audit = _priv_audit()
    neigh = {p.port: p.lldp_neighbor for p in audit.ports if p.lldp_neighbor}
    assert neigh == {
        "1/1": "gx-11-s74-wu", "1/2": "gx-11-s74-wu",
        "1/8": "gx-11-s72-p2", "1/16": "gx-11-s72-p2",
        "1/23": "gx-11-s74-wv", "1/24": "gx-11-s74-wv",
        "1/29": "gx-11-s59-p1",
    }
    assert not any("no LLDP neighbor data" in w for w in audit.warnings)


def test_priv_mlts_fully_up_and_uplink_detected():
    audit = _priv_audit()
    by_id = {m.mlt_id: m for m in audit.mlts}
    # every member port of every MLT is oper up on this box
    assert all(m.members_up == m.members_total for m in audit.mlts)
    assert not [w for w in audit.warnings if "member ports up" in w]
    # MLT 35 (1/1,1/2,1/23,1/24) faces the new gx-11-s74-* BEBs -> uplink
    assert by_id[35].members_up == 4
    assert by_id[35].is_uplink
    # single-port server MLTs are not uplinks
    assert not by_id[196].is_uplink
