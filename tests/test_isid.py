"""Per-VLAN I-SID resolution + decision worksheet (slice 2)."""

from switch_migrator.config import Config, SshSettings
from switch_migrator.isid import build_worksheet, candidates_for, resolve

PROD_ISID = 2500695       # VLAN 695 in prod, canonical prefix
LEGACY_PROD_ISID = 2510695   # the same, legacy 251 prefix


def _cfg(**over) -> Config:
    base = {"dvr_controllers": [], "core_switch_patterns": [],
                "isid_offsets": [2500000, 2510000, 2700000, 2710000],
                "isid_explicit": {}, "excluded_vlans": set(), "excluded_vlan_names": [],
                "ssh": SshSettings()}
    base.update(over)
    return Config(**base)


def test_candidates_span_all_offsets():
    assert candidates_for(695, _cfg()) == [2500695, 2510695, 2700695, 2710695]


def test_explicit_decision_wins():
    d = resolve(695, "svc", _cfg(isid_explicit={695: LEGACY_PROD_ISID}),
                matched_isid=PROD_ISID)
    assert d.isid == LEGACY_PROD_ISID
    assert d.source == "explicit"


def test_fabric_used_when_no_explicit():
    d = resolve(695, "svc", _cfg(), matched_isid=PROD_ISID)
    assert d.isid == PROD_ISID
    assert d.source == "fabric"
    assert not d.needs_decision


def test_unresolved_needs_decision_with_all_candidates():
    d = resolve(695, "svc", _cfg())
    assert d.isid is None
    assert d.needs_decision
    assert d.candidates == [2500695, 2510695, 2700695, 2710695]


def test_excluded_by_id_and_name():
    assert resolve(1, "Default", _cfg(excluded_vlans={1})).source == "excluded"
    d = resolve(99, "quarantine", _cfg(excluded_vlan_names=["quarant*"]))
    assert d.excluded
    assert d.isid is None


def test_worksheet_lists_decisions_and_paste_snippet():
    decisions = [
        resolve(695, "svc-a", _cfg()),                       # review
        resolve(735, "svc-b", _cfg(isid_explicit={735: 2510735})),  # explicit
        resolve(99, "quarantine", _cfg(excluded_vlan_names=["quarant*"])),  # excluded
    ]
    text = build_worksheet(decisions, device_name="ers-01")
    assert "1 need a decision" in text or "need a decision" in text
    assert "VLAN 695" in text
    assert "2500695 / 2510695 / 2700695 / 2710695" in text
    # ready-to-paste explicit snippet for the undecided VLAN
    assert "isid_conventions:" in text
    # nothing pre-filled: the line waits for a human (council review, Critic 5)
    assert "# 695: <ISID>    # candidates: 2500695, 2510695, 2700695, 2710695" in text
    assert "    695: 2500695" not in text
    # resolved ones are summarized, not asked about
    assert "VLAN 735: 2510735 (from explicit)" in text
    assert "VLAN 99: excluded" in text


def test_worksheet_without_offsets_never_prefills_a_fake_isid():
    decisions = [resolve(695, "svc-a", _cfg(isid_offsets=[]))]
    text = build_worksheet(decisions, device_name="ers-01")
    assert "695: 0" not in text                 # nothing is ever guessed
    assert "no offsets configured" in text
