"""Report tables for the migration-window features: the pre-migration health
check and the post-migration verification.

Kept apart from tables.py because these are not part of the audit - they are
produced by their own commands, at their own moment in the migration, and each
is a decision aid rather than an inventory.
"""

from __future__ import annotations

from switch_migrator import health, verify
from switch_migrator.report.tables import Table

_VERDICT_SEVERITY = {
    health.BLOCK: "error", health.WARN: "warn",
    health.OK: "ok", health.UNKNOWN: "warn",
}


def build_health(report: health.HealthReport) -> Table:
    """One row per finding, worst first - the go/no-go list."""
    t = Table("Health Check", ["Verdict", "Switch", "Check", "Finding",
                               "What it means"])
    for f in report.findings:
        t.add([f.verdict, f.switch, f.check, f.detail, f.action], f.severity)
    if not report.findings:
        t.add([health.OK, "(all)", "-", "no problem found on any switch", ""],
              "ok")
    return t


def build_health_summary(report: health.HealthReport) -> Table:
    t = Table("Health Summary", ["Switch", "Verdict", "Blockers", "Warnings"])
    for switch, verdict in sorted(report.by_switch.items()):
        blockers = sum(1 for f in report.findings
                       if f.switch == switch and f.verdict == health.BLOCK)
        warnings = sum(1 for f in report.findings
                       if f.switch == switch and f.verdict == health.WARN)
        t.add([switch, verdict, blockers or "", warnings or ""],
              _VERDICT_SEVERITY.get(verdict))
    return t


def build_verification(report: verify.VerifyReport) -> Table:
    """One row per link, failures first."""
    t = Table("Verification", [
        "Result", "Port ID", "Old switch", "Old port", "New switch",
        "New port", "Link", "MACs found/expected", "Neighbor expected",
        "Neighbor found", "VLANs expected", "VLANs found", "MLT expected",
        "MLT found", "Why",
    ])
    # a technician reads this on a laptop in a data centre: result, where the
    # link went, and what to do about it. The evidence columns are in the file.
    t.console_columns = ["Result", "Port ID", "Old switch", "Old port",
                         "New switch", "New port", "Why"]
    for v in report.verdicts:
        t.add([
            v.result, v.uid, v.old_switch, v.old_port, v.new_switch,
            v.new_port, v.link,
            f"{v.macs_found}/{v.macs_expected}" if v.macs_expected else "",
            v.neighbor_expected, v.neighbor_found,
            ",".join(map(str, v.vlans_expected)),
            ",".join(map(str, v.vlans_found)),
            v.mlt_expected if v.mlt_expected is not None else "",
            v.mlt_found if v.mlt_found is not None else "",
            v.why,
        ], v.severity)
    return t


def build_verification_summary(report: verify.VerifyReport,
                               extra_ports: list[tuple[str, str]]) -> Table:
    counts = report.counts()
    t = Table("Verification Summary", ["Item", "Count", "Meaning"])
    t.add(["PASS", counts[verify.PASS],
           "link up and an expected MAC (or the LLDP neighbor) reappeared"],
          "ok" if counts[verify.PASS] else None)
    t.add(["WARN", counts[verify.WARN],
           "link up but not yet confirmed - re-run in a few minutes, MACs "
           "age out and quiet hosts have not spoken"],
          "warn" if counts[verify.WARN] else None)
    t.add(["FAIL", counts[verify.FAIL],
           "link down, port missing, or the new switch could not be read"],
          "error" if counts[verify.FAIL] else None)
    t.add(["PENDING", counts[verify.PENDING],
           "no NEW switch/port recorded in the sheet yet"])
    if extra_ports:
        listed = ", ".join(f"{s} {p}" for s, p in extra_ports[:10])
        more = f" (+{len(extra_ports) - 10} more)" if len(extra_ports) > 10 else ""
        t.add(["Unlisted ports up", len(extra_ports),
               f"up on a new switch but in no sheet row: {listed}{more}"],
              "warn")
    return t
