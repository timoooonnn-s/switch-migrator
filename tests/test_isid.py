"""Per-VLAN I-SID resolution + decision worksheet (slice 2)."""

from switch_migrator.config import Config, SshSettings
from switch_migrator.isid import build_worksheet, candidates_for, resolve


def _cfg(**over) -> Config:
    base = dict(dvr_controllers=[], core_switch_patterns=[],
                isid_offsets=[2500000, 2510000, 2700000, 2710000],
                isid_explicit={}, excluded_vlans=set(), excluded_vlan_names=[],
                ssh=SshSettings())
    base.update(over)
    return Config(**base)


def test_candidates_span_all_offsets():
    assert candidates_for(695, _cfg()) == [2500695, 2510695, 2700695, 2710695]


def test_explicit_decision_wins():
    d = resolve(695, "svc", _cfg(isid_explicit={695: 2510695}), matched_isid=2500695)
    assert d.isid == 2510695 and d.source == "explicit"


def test_fabric_used_when_no_explicit():
    d = resolve(695, "svc", _cfg(), matched_isid=2500695)
    assert d.isid == 2500695 and d.source == "fabric" and not d.needs_decision


def test_unresolved_needs_decision_with_all_candidates():
    d = resolve(695, "svc", _cfg())
    assert d.isid is None and d.needs_decision
    assert d.candidates == [2500695, 2510695, 2700695, 2710695]


def test_excluded_by_id_and_name():
    assert resolve(1, "Default", _cfg(excluded_vlans={1})).source == "excluded"
    d = resolve(99, "quarantine", _cfg(excluded_vlan_names=["quarant*"]))
    assert d.excluded and d.isid is None


def test_worksheet_lists_decisions_and_paste_snippet():
    decisions = [
        resolve(695, "svc-a", _cfg()),                       # review
        resolve(735, "svc-b", _cfg(isid_explicit={735: 2510735})),  # explicit
        resolve(99, "quarantine", _cfg(excluded_vlan_names=["quarant*"])),  # excluded
    ]
    text = build_worksheet(decisions, device_name="ers-01")
    assert "1 need a decision" in text or "need a decision" in text
    assert "VLAN 695" in text and "2500695 / 2510695 / 2700695 / 2710695" in text
    # ready-to-paste explicit snippet for the undecided VLAN
    assert "isid_conventions:" in text and "695: 2500695" in text
    # resolved ones are summarized, not asked about
    assert "VLAN 735: 2510735 (from explicit)" in text
    assert "VLAN 99: excluded" in text
