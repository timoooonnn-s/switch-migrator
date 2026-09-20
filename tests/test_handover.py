"""The handover bundle: one folder somebody else can read.

The point of the bundle is that it survives leaving the machine that made it -
no tool, no network, no knowledge of which file is which.
"""

from switch_migrator import handover
from switch_migrator.models import Platform, SwitchAudit
from switch_migrator.report.tables import Table


def _audits() -> list[SwitchAudit]:
    ok = SwitchAudit(name="leaf-01", host="10.0.0.1", platform=Platform.VOSS,
                     reachable=True)
    ok.warnings.append("MLT 2: only 1/2 member ports up")
    dead = SwitchAudit(name="acc-09", host="10.0.0.9", platform=Platform.ERS,
                       reachable=False)
    dead.errors.append("connection refused")
    return [ok, dead]


def _tables() -> list[Table]:
    summary = Table("Summary", ["Switch", "Reachable"])
    summary.add(["leaf-01", "yes"], "ok")
    summary.add(["acc-09", "NO"], "error")
    issues = Table("Issues", ["Switch", "Kind", "Detail"])
    issues.add(["acc-09", "error", "connection refused"], "error")
    return [summary, issues, Table("Ports", ["Switch", "Port"])]


def _bundle(tmp_path):
    out = tmp_path / "output"
    (out / "config").mkdir(parents=True)
    (out / "migration-audit-20260101-000000.xlsx").write_bytes(b"xlsx")
    (out / "manifest-20260101-000000.json").write_text("{}")
    (out / "config" / "leaf-01.cfg").write_text("# extract\n")
    files = [out / "migration-audit-20260101-000000.xlsx",
             out / "manifest-20260101-000000.json",
             out / "config" / "leaf-01.cfg"]
    return out, handover.build(out, files, _tables(), _audits(),
                               meta={"config": "site-a.yaml"},
                               stamp="20260101-000000")


def test_bundle_gathers_every_file_and_an_index(tmp_path):
    out, bundle = _bundle(tmp_path)
    assert bundle == out / "handover-20260101-000000"
    names = sorted(p.name for p in bundle.iterdir())
    assert names == ["config", "index.html",
                     "manifest-20260101-000000.json",
                     "migration-audit-20260101-000000.xlsx"]
    assert (bundle / "config" / "leaf-01.cfg").read_text() == "# extract\n"


def test_the_originals_are_copied_not_moved(tmp_path):
    out, _ = _bundle(tmp_path)
    # an existing workflow that looks where the files were written still works
    assert (out / "migration-audit-20260101-000000.xlsx").is_file()
    assert (out / "config" / "leaf-01.cfg").is_file()


def test_config_extracts_are_not_copied_twice(tmp_path):
    _, bundle = _bundle(tmp_path)
    # once inside config/, never also loose at the top level
    assert not (bundle / "leaf-01.cfg").exists()


def test_index_is_self_contained_and_names_the_findings(tmp_path):
    _, bundle = _bundle(tmp_path)
    page = (bundle / "index.html").read_text()
    # no external assets: it has to open from a file share or a USB stick
    assert "http://" not in page and "https://" not in page
    assert "<style>" in page
    # the summary and the issues are readable without opening the workbook
    assert "connection refused" in page
    assert "1/2 reachable" in page
    # every file is linked by its own name
    assert 'href="manifest-20260101-000000.json"' in page
    # and each one says what it is
    assert "every command sent to every device" in page


def test_index_escapes_device_text(tmp_path):
    audits = _audits()
    audits[0].name = "leaf<script>alert(1)</script>"
    tables = _tables()
    tables[0].add(["<b>not markup</b>", "yes"], None)
    out = tmp_path / "output"
    out.mkdir()
    bundle = handover.build(out, [], tables, audits, stamp="20260101-000000")
    page = (bundle / "index.html").read_text()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;b&gt;not markup&lt;/b&gt;" in page


def test_a_missing_file_is_skipped_rather_than_fatal(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    bundle = handover.build(out, [out / "never-written.xlsx"], _tables(),
                            _audits(), stamp="20260101-000000")
    assert (bundle / "index.html").is_file()
    assert not (bundle / "never-written.xlsx").exists()


def test_the_config_directory_gets_a_blurb_like_every_other_entry(tmp_path):
    _, bundle = _bundle(tmp_path)
    page = (bundle / "index.html").read_text()
    assert "Per-device configuration" in page
