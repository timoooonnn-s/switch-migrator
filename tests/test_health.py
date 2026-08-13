"""Pre-migration health check: is this switch fit to be migrated tonight?"""

from switch_migrator import health, usage
from switch_migrator.models import (
    CompStatus,
    IstState,
    MltState,
    Platform,
    PortState,
    SwitchAudit,
    VlanComparison,
)


def _audit(**kw) -> SwitchAudit:
    base = dict(name="gx-01", host="gx-01", platform=Platform.VOSS,
                reachable=True)
    base.update(kw)
    audit = SwitchAudit(**base)
    if not audit.ports:
        # a healthy-looking baseline: one up uplink, one up access port
        audit.ports = [
            PortState(port="1/1", admin_up=True, oper_up=True, is_uplink=True,
                      lldp_neighbor="dvr-01"),
            PortState(port="1/2", admin_up=True, oper_up=True),
        ]
    if not audit.vlans:
        from switch_migrator.models import VlanInfo
        audit.vlans = [VlanInfo(vlan_id=100, name="users")]
    return audit


def _findings(report, check: str):
    return [f for f in report.findings if f.check == check]


def test_a_clean_switch_produces_nothing():
    report = health.check([_audit()])
    assert report.verdict == health.OK
    assert report.findings == []
    assert report.blockers == []


def test_a_degraded_mlt_blocks_the_migration():
    """The case the whole check exists for: an aggregation already running on
    one leg. Unplug the survivor and it is an outage, not a re-patch."""
    a = _audit()
    a.mlts = [MltState(mlt_id=35, name="srv-lag", members=["1/1", "1/2"],
                       members_up=1, in_datapath=True)]
    report = health.check([a])
    finding = _findings(report, "MLT 35")[0]
    assert finding.verdict == health.BLOCK
    assert "1/2" in finding.detail and "degraded" in finding.detail
    assert "no redundancy left" in finding.action
    assert report.verdict == health.BLOCK


def test_an_mlt_that_is_already_fully_down_is_a_warning_not_a_blocker():
    """Nothing is riding on it, so migrating cannot break it - but say so, or
    it gets blamed on the migration afterwards."""
    a = _audit()
    a.mlts = [MltState(mlt_id=35, name="dead", members=["1/1", "1/2"],
                       members_up=0)]
    finding = _findings(health.check([a]), "MLT 35")[0]
    assert finding.verdict == health.WARN
    assert "no member up" in finding.detail


def test_a_full_mlt_is_silent():
    a = _audit()
    a.mlts = [MltState(mlt_id=35, name="ok", members=["1/1", "1/2"],
                       members_up=2)]
    assert _findings(health.check([a]), "MLT 35") == []


def test_an_empty_mlt_id_is_not_a_finding():
    a = _audit()
    a.mlts = [MltState(mlt_id=9, name="", members=[], members_up=None)]
    assert health.check([a]).findings == []


def test_a_down_vist_blocks():
    a = _audit(ist=IstState(enabled=True, session_up=False,
                            peer_ip="10.41.8.10", vlan=31))
    finding = _findings(health.check([a]), "vIST")[0]
    assert finding.verdict == health.BLOCK
    assert "10.41.8.10" in finding.detail
    assert "SMLT pair is split" in finding.action


def test_an_up_vist_and_no_vist_are_both_fine():
    assert _findings(health.check([_audit(ist=IstState(enabled=True,
                                                       session_up=True))]),
                     "vIST") == []
    assert _findings(health.check([_audit(ist=IstState(enabled=False))]),
                     "vIST") == []
    assert _findings(health.check([_audit(ist=None)]), "vIST") == []


def test_an_unreadable_vist_state_is_unknown_not_a_pass():
    a = _audit(ist=IstState(enabled=True, session_up=None))
    finding = _findings(health.check([a]), "vIST")[0]
    assert finding.verdict == health.UNKNOWN


def test_losing_uplink_redundancy_blocks():
    a = _audit(ports=[
        PortState(port="1/1", oper_up=True, is_uplink=True),
        PortState(port="1/2", oper_up=False, is_uplink=True),
    ])
    finding = _findings(health.check([a]), "uplinks")[0]
    assert finding.verdict == health.BLOCK and "1 of 2" in finding.detail


def test_no_uplink_at_all_blocks():
    a = _audit(ports=[PortState(port="1/1", oper_up=False, is_uplink=True)])
    assert _findings(health.check([a]), "uplinks")[0].verdict == health.BLOCK


def test_no_identifiable_uplink_is_a_config_warning():
    a = _audit(ports=[PortState(port="1/2", oper_up=True)])
    finding = _findings(health.check([a]), "uplinks")[0]
    assert finding.verdict == health.WARN
    assert "core_switch_patterns" in finding.action


def test_an_unreachable_switch_blocks_and_stops_further_checks():
    a = SwitchAudit(name="gx-09", host="gx-09", platform=Platform.VOSS,
                    reachable=False, errors=["ConnectionFailed: timed out"])
    report = health.check([a])
    assert len(report.findings) == 1
    assert report.findings[0].verdict == health.BLOCK
    assert "timed out" in report.findings[0].detail


def test_no_port_state_blocks_because_nothing_can_be_verified_later():
    a = _audit(ports=[])
    a.ports = []
    finding = _findings(health.check([a]), "port state")[0]
    assert finding.verdict == health.BLOCK
    assert "nothing to verify against afterwards" in finding.action


def test_degraded_ports_are_reported_as_a_faulty_cable():
    a = _audit()
    a.ports += [PortState(port="1/9", oper_up=False, usage=usage.DEGRADED),
                PortState(port="1/10", oper_up=False, usage=usage.DEGRADED)]
    finding = _findings(health.check([a]), "degraded ports")[0]
    assert finding.verdict == health.WARN
    assert "1/9, 1/10" in finding.detail


def test_unused_ports_are_not_a_health_problem():
    a = _audit()
    a.ports += [PortState(port="1/9", oper_up=False, usage=usage.UNUSED)]
    assert _findings(health.check([a]), "degraded ports") == []


def test_vlans_with_nowhere_to_land_block():
    a = _audit()
    comps = [
        VlanComparison(switch="gx-01", vlan_id=695, vlan_name="x",
                       local_isid=None, expected_isids=[], dvr_isids=[],
                       matched_isid=None, status=CompStatus.MISSING_ON_DVR),
        VlanComparison(switch="gx-01", vlan_id=696, vlan_name="y",
                       local_isid=None, expected_isids=[], dvr_isids=[],
                       matched_isid=None, status=CompStatus.MISSING_ON_DVR),
        VlanComparison(switch="gx-01", vlan_id=100, vlan_name="ok",
                       local_isid=10100, expected_isids=[10100],
                       dvr_isids=[10100], matched_isid=10100,
                       status=CompStatus.OK),
    ]
    finding = _findings(health.check([a], {"gx-01": comps}), "VLAN vs fabric")[0]
    assert finding.verdict == health.BLOCK
    assert "695, 696" in finding.detail and "2 VLAN(s)" in finding.detail


def test_local_only_vlans_are_not_a_blocker():
    """VLAN 99 (quarantine) is local by design - the audit already settled
    this, and the health check must not re-open it."""
    a = _audit()
    comps = [VlanComparison(switch="gx-01", vlan_id=99, vlan_name="quarantine",
                            local_isid=None, expected_isids=[], dvr_isids=[],
                            matched_isid=None, status=CompStatus.LOCAL_ONLY)]
    assert _findings(health.check([a], {"gx-01": comps}), "VLAN vs fabric") == []


def test_session_warnings_mean_the_data_may_be_truncated():
    a = _audit(warnings=["device rejected 'terminal more disable' - long "
                         "command outputs may stall on this device"])
    finding = _findings(health.check([a]), "session")[0]
    assert finding.verdict == health.WARN
    assert "may be incomplete" in finding.action


def test_the_run_verdict_is_the_worst_switch():
    good = _audit(name="gx-01")
    bad = _audit(name="gx-02")
    bad.mlts = [MltState(mlt_id=1, name="lag", members=["1/1", "1/2"],
                         members_up=1)]
    report = health.check([good, bad])
    assert report.by_switch == {"gx-01": health.OK, "gx-02": health.BLOCK}
    assert report.verdict == health.BLOCK
    assert report.counts() == {health.BLOCK: 1, health.WARN: 0,
                               health.UNKNOWN: 0, health.OK: 1}


def test_findings_are_sorted_worst_first():
    a = _audit()
    a.mlts = [MltState(mlt_id=1, name="lag", members=["1/1", "1/2"],
                       members_up=1)]                      # BLOCK
    a.ports += [PortState(port="1/9", oper_up=False, usage=usage.DEGRADED)]  # WARN
    verdicts = [f.verdict for f in health.check([a]).findings]
    assert verdicts == sorted(verdicts, key=lambda v: health._RANK[v])
    assert verdicts[0] == health.BLOCK


def test_the_check_sends_no_commands():
    """It is derived from collected state only - there is no runner to send
    anything with, and that is the guarantee."""
    import inspect
    source = inspect.getsource(health)
    assert "runner" not in source.lower()
    assert "show " not in source
