"""Builds the report tables once; console/Excel/CSV all render the same data."""

from __future__ import annotations

from dataclasses import dataclass, field

from switch_migrator.models import (
    CompStatus,
    FabricState,
    SwitchAudit,
    VlanComparison,
)


@dataclass
class Table:
    title: str
    headers: list[str]
    rows: list[list] = field(default_factory=list)
    # per-row severity for coloring: ok / warn / error / None
    severities: list[str | None] = field(default_factory=list)

    def add(self, row: list, severity: str | None = None):
        self.rows.append(row)
        self.severities.append(severity)


def _fmt_bool(value: bool | None, true: str = "up", false: str = "down") -> str:
    if value is None:
        return "?"
    return true if value else false


def build_summary(audits: list[SwitchAudit],
                  comparisons: dict[str, list[VlanComparison]]) -> Table:
    t = Table("Summary", ["Switch", "Platform", "Reachable", "Ports up/total",
                          "Uplinks up/total", "MLTs", "MLT issues", "IST/vIST",
                          "VLANs", "VLAN OK", "VLAN warn", "VLAN error"])
    for a in audits:
        comps = [c for c in comparisons.get(a.name, [])
                 if c.status is not CompStatus.EXCLUDED]
        ok = sum(1 for c in comps if c.severity == "ok")
        warn = sum(1 for c in comps if c.severity == "warn")
        err = sum(1 for c in comps if c.severity == "error")
        mlt_issues = sum(1 for m in a.mlts if m.members and m.members_up < m.members_total)
        if not a.reachable:
            t.add([a.name, a.platform.value, "NO", "-", "-", "-", "-", "-",
                   "-", "-", "-", "-"], "error")
            continue
        ist = "-"
        if a.ist is not None:
            ist = _fmt_bool(a.ist.session_up).upper()
        severity = "error" if err or (a.ist and a.ist.session_up is False) \
            else ("warn" if warn or mlt_issues else "ok")
        t.add([a.name, a.platform.value, "yes",
               f"{a.ports_up}/{len(a.ports)}",
               f"{a.uplink_ports_up}/{a.uplink_ports_total}",
               len(a.mlts), mlt_issues, ist, len(comps), ok, warn, err],
              severity)
    return t


def build_ports(audits: list[SwitchAudit]) -> Table:
    t = Table("Ports", ["Switch", "Port", "Description", "Admin", "Oper",
                        "LLDP neighbor", "Uplink"])
    for a in audits:
        for p in a.ports:
            t.add([a.name, p.port, p.description,
                   _fmt_bool(p.admin_up, "enable", "disable"),
                   _fmt_bool(p.oper_up), p.lldp_neighbor,
                   "yes" if p.is_uplink else ""],
                  "warn" if p.is_uplink and not p.oper_up else None)
    return t


def build_mlts(audits: list[SwitchAudit]) -> Table:
    t = Table("MLTs", ["Switch", "MLT", "Name", "Type", "Admin", "Members",
                       "Members up", "IST", "Uplink"])
    for a in audits:
        for m in a.mlts:
            severity = None
            if m.members:
                if m.members_up == 0:
                    severity = "error"
                elif m.members_up < m.members_total:
                    severity = "warn"
            t.add([a.name, m.mlt_id, m.name, m.mlt_type, m.admin,
                   ",".join(m.members) or "-",
                   f"{m.members_up}/{m.members_total}",
                   "yes" if m.is_ist else "",
                   "yes" if m.is_uplink else ""], severity)
    return t


def build_vlan_comparison(comparisons: dict[str, list[VlanComparison]]) -> Table:
    t = Table("VLAN vs Fabric", ["Switch", "VLAN", "Name", "Local I-SID",
                                 "Expected I-SID(s)", "DvR attached I-SID(s)",
                                 "Matched I-SID", "Status", "Detail"])
    for comps in comparisons.values():
        for c in comps:
            if c.status is CompStatus.EXCLUDED:
                continue
            t.add([c.switch, c.vlan_id, c.vlan_name,
                   c.local_isid if c.local_isid is not None else "",
                   ",".join(map(str, c.expected_isids)),
                   ",".join(map(str, c.dvr_isids)) or "-",
                   c.matched_isid if c.matched_isid is not None else "-",
                   c.status.value, c.detail], c.severity)
    return t


def build_fabric(fabric: FabricState) -> Table:
    t = Table("Fabric I-SIDs", ["I-SID", "Name(s)", "Attached VLAN(s)",
                                "BEB host(s)", "Source(s)", "Seen on"])
    for isid in sorted(fabric.isids):
        rec = fabric.isids[isid]
        t.add([rec.isid, ",".join(sorted(rec.names)),
               ",".join(map(str, sorted(rec.cvids))),
               ",".join(sorted(rec.hosts)),
               ",".join(sorted(rec.sources)),
               ",".join(sorted(rec.seen_on))])
    return t


def build_issues(audits: list[SwitchAudit], fabric: FabricState,
                 comparisons: dict[str, list[VlanComparison]]) -> Table:
    t = Table("Issues", ["Source", "Severity", "Issue"])
    for err in fabric.dvr_errors:
        t.add(["DvR", "error", err], "error")
    for a in audits:
        for e in a.errors:
            t.add([a.name, "error", e], "error")
        for w in a.warnings:
            t.add([a.name, "warning", w], "warn")
    for comps in comparisons.values():
        for c in comps:
            if c.severity in ("warn", "error"):
                t.add([c.switch, "warning" if c.severity == "warn" else "error",
                       f"VLAN {c.vlan_id} [{c.status.value}]: {c.detail}"],
                      c.severity)
    return t


def build_all(audits: list[SwitchAudit], fabric: FabricState,
              comparisons: dict[str, list[VlanComparison]]) -> list[Table]:
    return [
        build_summary(audits, comparisons),
        build_vlan_comparison(comparisons),
        build_ports(audits),
        build_mlts(audits),
        build_fabric(fabric),
        build_issues(audits, fabric, comparisons),
    ]
