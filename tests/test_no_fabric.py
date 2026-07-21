"""--no-fabric inventory mode: collect and report per-switch state with no
DvR fabric collected and no comparison performed."""

import shutil
from pathlib import Path

import pytest

from switch_migrator.cli import main
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import FabricState, Platform
from switch_migrator.report.tables import build_all

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg() -> Config:
    return Config(dvr_controllers=[], core_switch_patterns=["dvr-*"],
                  isid_offsets=[], isid_explicit={}, excluded_vlans=set(),
                  ssh=SshSettings())


def _collect(name: str, platform: Platform, fixdir: str, raw_root: Path, cfg):
    shutil.copytree(FIXTURES / fixdir, raw_root / name)
    target = SwitchTarget(name, name, platform)
    return collect_switch(target, OfflineRunner(name, raw_root), cfg)


def test_vlan_membership_collected_both_platforms(tmp_path: Path, cfg: Config):
    voss = _collect("agg", Platform.VOSS, "voss", tmp_path / "v", cfg)
    ers = _collect("acc", Platform.ERS, "ers", tmp_path / "e", cfg)
    vmembers = {v.vlan_id: v.members for v in voss.vlans}
    assert vmembers.get(100) == ["1/1", "1/2", "2/1/1"]   # from show vlan members
    emembers = {v.vlan_id: v.members for v in ers.vlans}
    assert emembers[100] == ["1", "2", "3", "4", "5", "6", "7", "8", "10", "26"]
    # optional command never pollutes the report
    assert not [w for w in voss.warnings if "vlan members" in w]


def test_no_fabric_tables_have_inventory_not_comparison(tmp_path: Path, cfg: Config):
    voss = _collect("agg", Platform.VOSS, "voss", tmp_path / "v", cfg)
    tables = build_all([voss], FabricState(), {}, no_fabric=True)
    titles = [t.title for t in tables]
    assert "VLANs" in titles                    # inventory table present
    assert "VLAN vs Fabric" not in titles       # comparison table gone
    assert "Fabric I-SIDs" not in titles        # fabric table gone
    summary = next(t for t in tables if t.title == "Summary")
    assert "VLAN error" not in summary.headers  # comparison columns dropped
    vlans = next(t for t in tables if t.title == "VLANs")
    # the VLAN <-> I-SID overview: mapping columns present, no DvR/verdict
    assert vlans.headers == ["Switch", "VLAN", "Name", "I-SID", "I-SID name",
                             "Member ports", "# ports"]
    row100 = next(r for r in vlans.rows if r[1] == 100)
    # VLAN name (from 'show vlan basic') and I-SID name are shown SEPARATELY:
    # real VLAN name 'Users' vs I-SID name 'Server-VLAN-100'
    assert row100[2] == "Users"                # VLAN name
    assert row100[3] == 10100                   # I-SID
    assert row100[4] == "Server-VLAN-100"       # I-SID name
    assert row100[5] == "1/1,1/2,2/1/1"         # member ports from show vlan members


def test_cli_no_fabric_end_to_end(tmp_path: Path):
    # a config with NO dvr_controllers and NO conventions must run under
    # --no-fabric and exit cleanly (0 = no error-severity findings on this
    # healthy VOSS capture), producing an inventory report but no fabric tables
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "voss", raw / "iso-voss-01")
    (tmp_path / "config.yaml").write_text('excluded_vlan_names: ["quarant*"]\n')
    out = tmp_path / "out"
    rc = main([
        "-c", str(tmp_path / "config.yaml"), "--no-fabric", "--offline", str(raw),
        "-s", "iso-voss-01:voss", "-o", str(out), "--csv", "--no-excel",
    ])
    assert rc == 0
    names = {p.name for p in out.glob("csv-*/*.csv")}
    assert "vlans.csv" in names                  # inventory table exported
    assert "vlan_vs_fabric.csv" not in names     # comparison table gone
    assert "fabric_i_sids.csv" not in names      # fabric table gone
