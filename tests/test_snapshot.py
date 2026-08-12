"""Snapshot persistence: collect once, report as often as you like."""

import json
import shutil
from pathlib import Path

import pytest

from switch_migrator import snapshot
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import FabricState, Platform, SwitchAudit

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg() -> Config:
    return Config(dvr_controllers=[], core_switch_patterns=["dvr-*"],
                  isid_offsets=[10000], isid_explicit={}, excluded_vlans={1},
                  ssh=SshSettings())


@pytest.fixture
def collected(tmp_path, cfg):
    shutil.copytree(FIXTURES / "voss", tmp_path / "sw1")
    shutil.copytree(FIXTURES / "ers", tmp_path / "sw2")
    audits = [
        collect_switch(SwitchTarget("sw1", "sw1", Platform.VOSS),
                       OfflineRunner("sw1", tmp_path), cfg, pull_macs=True),
        collect_switch(SwitchTarget("sw2", "sw2", Platform.ERS),
                       OfflineRunner("sw2", tmp_path), cfg, pull_macs=True),
    ]
    fabric = FabricState()
    rec = fabric.get_or_create(10100)
    rec.cvids, rec.names, rec.seen_on = {100}, {"users"}, {"dvr-01"}
    fabric.dvrs_ok = ["dvr-01"]
    fabric.dvr_errors = ["dvr-02: unreachable"]
    return audits, fabric


def test_round_trip_is_lossless(tmp_path, collected):
    """The whole point: what comes back must be what went in, or a report built
    from a snapshot would quietly differ from the one built live."""
    audits, fabric = collected
    path = snapshot.save(tmp_path / "s.json", audits, fabric)
    back, back_fabric, meta = snapshot.load(path)

    assert snapshot._plain(back) == snapshot._plain(audits)
    assert snapshot._plain(back_fabric) == snapshot._plain(fabric)
    assert meta["tool_version"]


def test_types_survive_json(tmp_path, collected):
    # JSON has no enums, sets or tuples - every one of them must come back as
    # the type the rest of the code expects to work with
    audits, fabric = collected
    back, back_fabric, _ = snapshot.load(
        snapshot.save(tmp_path / "s.json", audits, fabric))
    voss = next(a for a in back if a.name == "sw1")
    assert isinstance(voss, SwitchAudit)
    assert voss.platform is Platform.VOSS
    assert voss.ports and isinstance(voss.ports[0].vlans, list)
    assert voss.mlts and isinstance(voss.mlts[0].members, list)
    assert voss.ist is not None and voss.ist.enabled is True
    assert voss.vlans and voss.vlans[0].vlan_id == 1
    isid = back_fabric.isids[10100]
    assert isid.cvids == {100} and isid.seen_on == {"dvr-01"}
    assert back_fabric.dvrs_ok == ["dvr-01"]
    # derived properties still work on the rebuilt objects
    assert voss.ports_up == sum(1 for p in voss.ports if p.oper_up)


def test_usage_classification_survives(tmp_path, collected):
    audits, fabric = collected
    back, _, _ = snapshot.load(snapshot.save(tmp_path / "s.json", audits, fabric))
    before = {(a.name, p.port): (p.usage, p.usage_evidence, p.last_change_days)
              for a in audits for p in a.ports}
    after = {(a.name, p.port): (p.usage, p.usage_evidence, p.last_change_days)
             for a in back for p in a.ports}
    assert before == after and before


def test_missing_fields_fall_back_to_defaults(tmp_path):
    """A snapshot written by an older build still opens."""
    thin = {
        "format": snapshot.SNAPSHOT_FORMAT,
        "tool_version": "0.9.0",
        "created": "2026-01-01T00:00:00",
        "fabric": {},
        "switches": [{"name": "old", "host": "old", "platform": "ers"}],
    }
    path = tmp_path / "thin.json"
    path.write_text(json.dumps(thin))
    audits, fabric, meta = snapshot.load(path)
    assert audits[0].name == "old" and audits[0].platform is Platform.ERS
    assert audits[0].ports == [] and audits[0].reachable is False
    assert fabric.isids == {} and meta["tool_version"] == "0.9.0"


def test_unknown_fields_are_ignored(tmp_path, collected):
    """...and one written by a NEWER build does not crash this one."""
    audits, fabric = collected
    path = snapshot.save(tmp_path / "s.json", audits, fabric)
    data = json.loads(path.read_text())
    data["switches"][0]["a_field_from_the_future"] = {"x": 1}
    path.write_text(json.dumps(data))
    back, _, _ = snapshot.load(path)
    assert back[0].name == audits[0].name


def test_a_different_format_version_is_refused(tmp_path, collected):
    audits, fabric = collected
    path = snapshot.save(tmp_path / "s.json", audits, fabric)
    data = json.loads(path.read_text())
    data["format"] = snapshot.SNAPSHOT_FORMAT + 1
    path.write_text(json.dumps(data))
    with pytest.raises(snapshot.SnapshotError, match="format"):
        snapshot.load(path)


def test_clear_errors_for_junk_input(tmp_path):
    with pytest.raises(snapshot.SnapshotError, match="not found"):
        snapshot.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(snapshot.SnapshotError, match="valid JSON"):
        snapshot.load(bad)
    other = tmp_path / "other.json"
    other.write_text('{"something": "else"}')
    with pytest.raises(snapshot.SnapshotError, match="not a switch-migrator"):
        snapshot.load(other)
