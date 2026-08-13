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
    # MLT 2 has no members: flagged as dead, nothing to recreate
    assert any("MLT 2" in w and "DEAD MLT" in w for w in ers.warnings)

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


def test_snapshot_run_reproduces_the_live_reports_exactly(tmp_path):
    """The promise of a snapshot: the sheets you get from the file are the
    sheets you would have got from the switches."""
    import shutil
    from switch_migrator.cli import main

    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copytree(FIXTURES / "voss", raw / "sw-voss")
    shutil.copytree(FIXTURES / "ers", raw / "sw-ers")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("excluded_vlan_names: ['quarant*']\n")

    live = tmp_path / "live"
    common = ["-c", str(cfg), "--offline", str(raw), "-s", "sw-voss:voss",
              "-s", "sw-ers:ers", "--no-fabric", "--migration-sheets",
              "--csv", "--no-excel"]
    snap = tmp_path / "snap.json"
    main(common + ["-o", str(live), "--save-snapshot", str(snap)])
    assert snap.is_file()

    replay = tmp_path / "replay"
    main(["-c", str(cfg), "--from-snapshot", str(snap), "-o", str(replay),
          "--no-fabric", "--migration-sheets", "--csv", "--no-excel"])

    live_csv = next(live.glob("csv-*"))
    replay_csv = next(replay.glob("csv-*"))
    produced = sorted(p.name for p in live_csv.glob("*.csv"))
    assert "cabling.csv" in produced and "port_info.csv" in produced
    for name in produced:
        assert (live_csv / name).read_text() == (replay_csv / name).read_text(), name


def _filled_sheet(path: Path, rows: list[dict]) -> Path:
    """A cabling sheet as it comes back from the data centre."""
    import csv
    headers = ["Port ID", "Type", "End device / neighbor", "NEW switch",
               "NEW port", "NEW MLT ID", "NEW MLT name", "Old switch",
               "Old port", "MLT ID", "MLT name", "Port VLANs", "MAC addresses",
               "Done by"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])
    return path


def test_the_whole_migration_arc(tmp_path):
    """Health check before, MLT blocks from the sheet, verification after -
    driven through the real CLI the way a migration night would."""
    import csv
    import shutil
    from switch_migrator.cli import main

    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copytree(FIXTURES / "voss", raw / "gx-01")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("core_switch_patterns: ['core-*', 'dvr-*']\n")
    common = ["-c", str(cfg), "--offline", str(raw), "--no-fabric", "--no-excel"]

    # 1) before the window: the fixture switch has MLTs with a down member,
    #    so the health check must refuse to call it clean
    code = main(common + ["-o", str(tmp_path / "pre"), "-s", "gx-01:voss",
                          "--health-check"])
    assert code == 1, "a degraded MLT has to be a non-zero exit"

    # 2) the sheet comes back filled in; generate the new switch's MLT blocks
    sheet = _filled_sheet(tmp_path / "cabling.csv", [
        {"Port ID": "P0001", "Type": "mlt", "Old switch": "gx-01",
         "Old port": "1/1", "NEW switch": "gx-01", "NEW port": "1/1",
         "MLT ID": "2", "MLT name": "MLT002", "NEW MLT ID": "42",
         "NEW MLT name": "new-lag", "End device / neighbor": "core-01",
         "Port VLANs": "100", "Done by": "TK"},
        {"Port ID": "P0002", "Type": "mlt", "Old switch": "gx-01",
         "Old port": "1/2", "NEW switch": "gx-01", "NEW port": "1/2",
         "MLT ID": "2", "MLT name": "MLT002", "NEW MLT ID": "42",
         "NEW MLT name": "new-lag", "Port VLANs": "100", "Done by": "TK"},
        {"Port ID": "P0003", "Old switch": "gx-01", "Old port": "1/47"},
    ])
    out = tmp_path / "mlt"
    assert main(["-c", str(cfg), "-o", str(out), "--generate-mlt",
                 str(sheet)]) == 0
    blocks = (out / "config" / "mlt-blocks.cfg").read_text()
    assert 'mlt 42 enable name "new-lag"' in blocks
    assert "mlt 42 member 1/1,1/2" in blocks
    assert "interface mlt 42" in blocks and "smlt" in blocks

    # 3) after the window: verify against the (replayed) new switch. The
    #    switches to check come from the sheet itself.
    code = main(["-c", str(cfg), "--offline", str(raw), "--no-fabric",
                 "--no-excel", "-o", str(tmp_path / "post"), "--csv",
                 "--verify-migration", str(sheet)])
    verification = next((tmp_path / "post").glob("csv-*/verification.csv"))
    rows = {r["Port ID"]: r for r in
            csv.DictReader(verification.open())}
    # 1/1 is up and its LLDP neighbor is the one the sheet recorded, but the
    # port is still in the OLD MLT - exactly the half-done state a warn is for
    assert rows["P0001"]["Result"] == "WARN"
    assert "LLDP neighbor matches" in rows["P0001"]["Why"]
    assert "sheet says MLT 42" in rows["P0001"]["Why"]
    # 1/2 is down in the capture: a link that did not come back is a failure
    assert rows["P0002"]["Result"] == "FAIL"
    assert "DOWN" in rows["P0002"]["Why"]
    # and the row nobody filled in is neither a pass nor a problem
    assert rows["P0003"]["Result"] == "PENDING"
    assert code == 1, "a failed link has to be a non-zero exit"

    summary = next((tmp_path / "post").glob("csv-*/verification_summary.csv"))
    text = summary.read_text()
    assert "Unlisted ports up" in text     # 1/47 and 2/1/1 are up, unlisted
