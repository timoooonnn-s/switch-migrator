from switch_migrator.compare import compare_switch, expected_isids
from switch_migrator.config import Config, SshSettings
from switch_migrator.models import (
    CompStatus,
    FabricState,
    Platform,
    SwitchAudit,
    VlanInfo,
)


def make_config(**overrides) -> Config:
    defaults = dict(
        dvr_controllers=[],
        core_switch_patterns=["dvr-*"],
        isid_offsets=[10000, 20000],
        isid_explicit={},
        excluded_vlans={1, 4000},
        ssh=SshSettings(),
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_fabric(entries: dict[int, dict]) -> FabricState:
    fabric = FabricState()
    for isid, info in entries.items():
        rec = fabric.get_or_create(isid)
        rec.cvids.update(info.get("cvids", []))
        rec.hosts.update(info.get("hosts", []))
    fabric.dvrs_ok = ["dvr-01"]
    return fabric


def make_audit(platform: Platform, vlans: list[VlanInfo]) -> SwitchAudit:
    return SwitchAudit(name="sw", host="sw", platform=platform,
                       reachable=True, vlans=vlans)


def by_vlan(results):
    return {r.vlan_id: r for r in results}


def test_expected_isids_offsets_and_explicit():
    cfg = make_config(isid_explicit={300: 99300})
    assert expected_isids(100, cfg) == [10100, 20100]
    assert expected_isids(300, cfg) == [99300]


def test_ers_convention_confirmed_by_dvr():
    cfg = make_config()
    fabric = make_fabric({10100: {"cvids": [100]}})
    audit = make_audit(Platform.ERS, [VlanInfo(100, "Users")])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.OK
    assert res[100].matched_isid == 10100


def test_ers_dvr_only_nonstandard():
    cfg = make_config()
    fabric = make_fabric({77777: {"cvids": [100]}})
    audit = make_audit(Platform.ERS, [VlanInfo(100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.OK_NONSTANDARD
    assert res[100].matched_isid == 77777


def test_ers_in_fabric_not_attached():
    cfg = make_config()
    fabric = make_fabric({10100: {"cvids": []}})
    audit = make_audit(Platform.ERS, [VlanInfo(100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.IN_FABRIC_NOT_ATTACHED
    assert res[100].matched_isid == 10100


def test_ers_ambiguous_multiple_candidates():
    cfg = make_config()
    fabric = make_fabric({10100: {}, 20100: {}})
    audit = make_audit(Platform.ERS, [VlanInfo(100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.AMBIGUOUS


def test_ers_missing():
    cfg = make_config()
    fabric = make_fabric({})
    audit = make_audit(Platform.ERS, [VlanInfo(666, "Orphan")])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[666].status is CompStatus.MISSING_ON_DVR


def test_excluded_vlan():
    cfg = make_config()
    fabric = make_fabric({})
    audit = make_audit(Platform.ERS, [VlanInfo(1), VlanInfo(4000)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[1].status is CompStatus.EXCLUDED
    assert res[4000].status is CompStatus.EXCLUDED


def test_excluded_vlan_by_name_pattern():
    # quarantine VLAN 99: intentionally absent from the fabric, excluded by
    # its NAME - the detail must say on which switch it exists
    cfg = make_config(excluded_vlan_names=["quarant*"])
    fabric = make_fabric({})
    audit = make_audit(Platform.ERS, [VlanInfo(99, "Quarantaine"),
                                      VlanInfo(666, "Orphan")])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[99].status is CompStatus.EXCLUDED
    assert "Quarantaine" in res[99].detail
    assert "exists on sw" in res[99].detail
    # non-matching VLANs still get real verdicts
    assert res[666].status is CompStatus.MISSING_ON_DVR


def test_explicit_mapping_wins():
    cfg = make_config(isid_explicit={100: 55555})
    fabric = make_fabric({55555: {"cvids": [100]}, 10100: {"cvids": [100]}})
    audit = make_audit(Platform.ERS, [VlanInfo(100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.OK
    assert res[100].matched_isid == 55555


def test_voss_local_binding_ok():
    cfg = make_config()
    fabric = make_fabric({10100: {"cvids": [100]}})
    audit = make_audit(Platform.VOSS, [VlanInfo(100, isid=10100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.OK


def test_voss_local_binding_nonstandard():
    cfg = make_config()
    fabric = make_fabric({77777: {}})
    audit = make_audit(Platform.VOSS, [VlanInfo(300, isid=77777)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[300].status is CompStatus.OK_NONSTANDARD


def test_voss_local_binding_conflicts_with_dvr():
    # the switch says VLAN 100 -> 10100, the DvR controllers attach VLAN 100
    # to a different I-SID: that disagreement must be flagged, not passed as OK
    cfg = make_config()
    fabric = make_fabric({10100: {}, 20100: {"cvids": [100]}})
    audit = make_audit(Platform.VOSS, [VlanInfo(100, isid=10100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.LOCAL_BINDING_CONFLICT
    assert res[100].severity == "error"


def test_voss_local_binding_agrees_with_dvr():
    cfg = make_config()
    fabric = make_fabric({10100: {"cvids": [100]}})
    audit = make_audit(Platform.VOSS, [VlanInfo(100, isid=10100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.OK


def test_voss_local_binding_not_in_fabric():
    cfg = make_config()
    fabric = make_fabric({})
    audit = make_audit(Platform.VOSS, [VlanInfo(100, isid=10100)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[100].status is CompStatus.LOCAL_ISID_NOT_IN_FABRIC


def test_voss_vlan_without_isid_falls_back_to_derivation():
    cfg = make_config()
    fabric = make_fabric({10200: {"cvids": [200]}})
    audit = make_audit(Platform.VOSS, [VlanInfo(200, isid=None)])
    res = by_vlan(compare_switch(audit, fabric, cfg))
    assert res[200].status is CompStatus.OK
    assert res[200].matched_isid == 10200
