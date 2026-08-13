"""Port usage classification: is this port actually in use, or just down?"""

from datetime import datetime, timedelta

from switch_migrator import usage as U
from switch_migrator.models import MltState, Platform, PortState, SwitchAudit

NOW = datetime(2026, 8, 12, 12, 0, 0)


def _port(**kw) -> PortState:
    base = dict(port="1/1", admin_up=True, oper_up=False)
    base.update(kw)
    return PortState(**base)


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%m/%d/%y %H:%M:%S")


# --------------------------- date parsing ----------------------------------

def test_last_change_days_from_real_format():
    # real DATE column is MM/DD/YY HH:MM:SS ('05/13/26' -> 13 can only be a day)
    assert U.parse_last_change_days("08/02/26 08:13:38", now=NOW) == 10
    assert U.parse_last_change_days("05/13/26 15:54:53", now=NOW) == 90  # full days
    assert U.parse_last_change_days("", now=NOW) is None
    assert U.parse_last_change_days("garbage", now=NOW) is None


def test_uptime_days_from_sys_info():
    assert U.parse_uptime_days("        SysUpTime    : 135 day(s), 07:12:09") == 135
    assert U.parse_uptime_days("no uptime here") is None


# --------------------------- classification --------------------------------

def test_link_up_is_in_use():
    cls, why = U.classify_port(_port(oper_up=True, lldp_neighbor="srv-a"), None)
    assert cls == U.IN_USE and "link up" in why and "srv-a" in why


def test_down_but_macs_learned_is_in_use():
    cls, _ = U.classify_port(_port(macs=["00:11:22:33:44:55"], mac_total=1), None)
    assert cls == U.IN_USE


def test_down_member_of_forwarding_mlt_is_degraded_not_unused():
    """The case that started this: one link of an MLT is down, the MLT still
    forwards. That is a FAULT on a live cable, never an unused port."""
    mlt = MltState(mlt_id=35, name="uplink", members=["1/1", "1/2"],
                   members_up=1, in_datapath=True)
    port = _port(mlt_id=35, last_change_days=200, has_traffic=False)
    cls, why = U.classify_port(port, mlt)
    assert cls == U.DEGRADED
    assert "MLT 35 still forwarding" in why and "1/2 up" in why


def test_down_long_with_no_traffic_is_unused():
    port = _port(last_change_days=120, has_traffic=False)
    cls, why = U.classify_port(port, None, unused_after_days=30, uptime_days=200)
    assert cls == U.UNUSED
    assert "120d" in why and "0 traffic" in why


def test_down_long_but_has_traffic_is_uncertain():
    # counters prove the port carried traffic at some point -> do not write it off
    port = _port(last_change_days=120, has_traffic=True)
    cls, why = U.classify_port(port, None, unused_after_days=30)
    assert cls == U.UNCERTAIN and "has passed traffic" in why


def test_recently_down_is_uncertain_not_unused():
    # a server rebooting / a link flapping must not be filtered away
    port = _port(last_change_days=2, has_traffic=False)
    cls, why = U.classify_port(port, None, unused_after_days=30)
    assert cls == U.UNCERTAIN and "only 2d" in why


def test_zero_counters_on_freshly_rebooted_box_are_not_trusted():
    # '0 packets' means nothing if the counters only started 3 days ago
    port = _port(last_change_days=120, has_traffic=False)
    cls, _ = U.classify_port(port, None, unused_after_days=30, uptime_days=3)
    assert cls == U.LIKELY_UNUSED          # not the confident UNUSED
    cls, _ = U.classify_port(port, None, unused_after_days=30, uptime_days=300)
    assert cls == U.UNUSED


def test_unknown_age_stays_uncertain():
    cls, why = U.classify_port(_port(last_change_days=None), None)
    assert cls == U.UNCERTAIN and "unknown" in why


def test_admin_disabled_long_down_is_unused():
    port = _port(admin_up=False, last_change_days=200, has_traffic=False)
    cls, why = U.classify_port(port, None, uptime_days=300)
    assert cls == U.UNUSED and "admin disabled" in why


# --------------------------- audit-level -----------------------------------

def test_classify_audit_parses_dates_and_uses_mlts():
    a = SwitchAudit(name="sw", host="sw", platform=Platform.VOSS, reachable=True)
    a.mlts = [MltState(mlt_id=1, name="up", members=["1/1", "1/2"],
                       members_up=1, in_datapath=True)]
    a.ports = [
        _port(port="1/1", oper_up=True, mlt_id=1),
        _port(port="1/2", mlt_id=1, last_change=_days_ago(1)),   # flapped member
        _port(port="1/9", last_change=_days_ago(200), has_traffic=False),
    ]
    U.classify_audit(a, unused_after_days=30, uptime_days=300, now=NOW)
    by = {p.port: p for p in a.ports}
    assert by["1/1"].usage == U.IN_USE
    assert by["1/2"].usage == U.DEGRADED          # down member of a live MLT
    assert by["1/9"].usage == U.UNUSED
    assert by["1/9"].last_change_days == 200      # parsed from the DATE column
