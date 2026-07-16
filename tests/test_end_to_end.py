"""End-to-end: replay the fixture CLI outputs through the real collectors,
comparison engine and report writers using the OfflineRunner."""

import shutil
from pathlib import Path

import pytest

from switch_migrator.collectors.dvr import collect_dvr
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch
from switch_migrator.config import Config, SshSettings, SwitchTarget
from switch_migrator.connection import OfflineRunner
from switch_migrator.models import CompStatus, FabricState, Platform
from switch_migrator.report.excel import write_csv, write_excel
from switch_migrator.report.tables import build_all

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES / "voss", tmp_path / "old-agg-01")
    shutil.copytree(FIXTURES / "ers", tmp_path / "old-access-01")
    shutil.copytree(FIXTURES / "voss", tmp_path / "dvr-01")
    return tmp_path


@pytest.fixture
def cfg() -> Config:
    return Config(
        dvr_controllers=[],
        core_switch_patterns=["dvr-*", "core-*"],
        isid_offsets=[10000, 20000],
        isid_explicit={},
        excluded_vlans={1, 4000},
        ssh=SshSettings(),
    )


def test_full_pipeline(raw_root: Path, cfg: Config, tmp_path: Path):
    fabric = FabricState()
    collect_dvr("dvr-01", OfflineRunner("dvr-01", raw_root), fabric)
    assert fabric.dvrs_ok == ["dvr-01"]
    # from show dvr interfaces + show isis spbm i-sid all + show i-sid + show vlan i-sid
    assert {10100, 10200, 20300, 20400, 77777, 1501050} <= set(fabric.isids)
    assert 100 in fabric.isids[10100].cvids
    assert 300 in fabric.isids[20300].cvids
    # DvR interface data: L2ISID 1501050 carries VLAN 1050 domain-wide
    assert fabric.isids[1501050].cvids == {1050}
    assert "dvr" in fabric.isids[10100].sources

    voss_target = SwitchTarget("old-agg-01", "old-agg-01", Platform.VOSS)
    ers_target = SwitchTarget("old-access-01", "old-access-01", Platform.ERS)
    voss = collect_switch(voss_target, OfflineRunner("old-agg-01", raw_root), cfg)
    ers = collect_switch(ers_target, OfflineRunner("old-access-01", raw_root), cfg)

    # VOSS switch state (ports from `show int gig state`, incl. down reason)
    assert voss.ports_up == 3
    assert {p.port: p.state_reason for p in voss.ports}["1/48"] == "SSH"
    assert voss.ist and voss.ist.session_up is True
    assert [m.mlt_id for m in voss.mlts] == [1, 2, 10]  # no phantom footer MLT
    ist_mlt = voss.mlts[0]
    assert ist_mlt.members_up == 1  # 1/47 up, 1/48 down
    assert any("MLT 1" in w for w in voss.warnings)
    # `show int gig i-sid` merge agrees with `show vlan i-sid` -> no conflicts
    assert not any("binds I-SID" in w for w in voss.warnings)

    # ERS switch: uplink detection via LLDP against dvr-* pattern
    uplinks = [p for p in ers.ports if p.is_uplink]
    assert [p.port for p in uplinks] == ["49", "50"]
    uplink_mlt = next(m for m in ers.mlts if m.mlt_id == 1)
    assert uplink_mlt.is_uplink
    assert uplink_mlt.members_up == 2

    comparisons = {
        a.name: compare_switch(a, fabric, cfg) for a in (voss, ers)
    }
    ers_res = {c.vlan_id: c for c in comparisons["old-access-01"]}
    assert ers_res[100].status is CompStatus.OK          # 10100 attached c-vid 100
    assert ers_res[200].status is CompStatus.OK          # 10200 attached c-vid 200
    assert ers_res[300].status is CompStatus.OK          # 20300 attached c-vid 300
    assert ers_res[666].status is CompStatus.MISSING_ON_DVR
    assert ers_res[1].status is CompStatus.EXCLUDED

    voss_res = {c.vlan_id: c for c in comparisons["old-agg-01"]}
    assert voss_res[100].status is CompStatus.OK
    assert voss_res[300].status is CompStatus.OK_NONSTANDARD  # bound to 77777

    tables = build_all([ers, voss], fabric, comparisons)
    xlsx = tmp_path / "report.xlsx"
    write_excel(tables, xlsx)
    assert xlsx.stat().st_size > 0
    csvs = write_csv(tables, tmp_path / "csv")
    assert len(csvs) == len(tables)
