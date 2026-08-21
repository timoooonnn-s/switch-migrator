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
    # Headers worth showing on a terminal. The migration sheets are deliberately
    # wide - twenty columns is fine on a printed page and unreadable in an
    # 80-column shell, where rich squeezes every column to three characters.
    # Excel and CSV always get every column; None means "show them all".
    console_columns: list[str] | None = None

    # --- presentation hints, honoured by the Excel writer -------------------
    # Columns whose content is long enough to need wrapping rather than being
    # cut off at the column width (VLAN pair lists, MAC lists, evidence).
    wrap_columns: list[str] = field(default_factory=list)
    # Columns a human fills in by hand. They are left unlocked when the sheet
    # is protected, and shaded so the eye finds them on a printed page.
    manual_columns: list[str] = field(default_factory=list)
    # {column: allowed values} - a dropdown, so a switch name is picked rather
    # than typed at 3am.
    choice_columns: dict[str, list[str]] = field(default_factory=dict)
    # Columns that must hold a whole number if they hold anything.
    int_columns: list[str] = field(default_factory=list)
    # Start a new printed page whenever this column's value changes, so one
    # rack's rows never straddle two sheets of paper.
    page_break_column: str = ""
    # How many leading columns stay on screen while scrolling right.
    freeze_columns: int = 0

    def add(self, row: list, severity: str | None = None):
        self.rows.append(row)
        self.severities.append(severity)

    def for_console(self) -> tuple[list[str], list[list]]:
        """(headers, rows) reduced to the console subset."""
        if not self.console_columns:
            return self.headers, self.rows
        keep = [i for i, h in enumerate(self.headers)
                if h in self.console_columns]
        return ([self.headers[i] for i in keep],
                [[row[i] for i in keep] for row in self.rows])


def _fmt_bool(value: bool | None, true: str = "up", false: str = "down") -> str:
    if value is None:
        return "?"
    return true if value else false


def _mlt_up_severity(m) -> str | None:
    """Severity for one MLT's member-up state, tolerant of unknown counts."""
    if not m.members:
        return None                       # dead MLT: handled/labelled separately
    if m.members_up is None:              # no port state - lean on the data-path table
        return "warn" if m.in_datapath is False else None
    if m.members_up == 0:
        return "error"
    if m.members_up < m.members_total:
        return "warn"
    return None


def build_summary(audits: list[SwitchAudit],
                  comparisons: dict[str, list[VlanComparison]],
                  no_fabric: bool = False) -> Table:
    headers = ["Switch", "Platform", "Reachable", "Ports up/total",
               "Uplinks up/total", "MLTs", "MLT issues", "IST/vIST", "VLANs"]
    if not no_fabric:
        headers += ["VLAN OK", "VLAN warn", "VLAN error"]
    t = Table("Summary", headers)
    for a in audits:
        comps = [c for c in comparisons.get(a.name, [])
                 if c.status is not CompStatus.EXCLUDED]
        ok = sum(1 for c in comps if c.severity == "ok")
        warn = sum(1 for c in comps if c.severity == "warn")
        err = sum(1 for c in comps if c.severity == "error")
        mlt_issues = sum(1 for m in a.mlts if _mlt_up_severity(m) in ("warn", "error"))
        # in no-fabric mode the VLAN count is just how many VLANs the switch has
        vlan_count = len(a.vlans) if no_fabric else len(comps)
        if not a.reachable:
            row = [a.name, a.platform.value, "NO", "-", "-", "-", "-", "-", "-"]
            t.add(row + ([] if no_fabric else ["-", "-", "-"]), "error")
            continue
        ist = "-"
        if a.ist is not None:
            ist = _fmt_bool(a.ist.session_up).upper()
        severity = "error" if err or (a.ist and a.ist.session_up is False) \
            else ("warn" if warn or mlt_issues else "ok")
        row = [a.name, a.platform.value, "yes",
               f"{a.ports_up}/{len(a.ports)}",
               f"{a.uplink_ports_up}/{a.uplink_ports_total}",
               len(a.mlts), mlt_issues, ist, vlan_count]
        t.add(row + ([] if no_fabric else [ok, warn, err]), severity)
    return t


def build_vlan_inventory(audits: list[SwitchAudit]) -> Table:
    """Per-switch VLAN <-> I-SID overview, no fabric comparison (--no-fabric).

    Same shape as 'VLAN vs Fabric' minus the DvR/verdict columns: which VLAN
    carries which I-SID (and its name), plus the configured member ports.
    """
    t = Table("VLANs", ["Switch", "VLAN", "Name", "I-SID", "I-SID name",
                        "Member ports", "# ports"])
    for a in audits:
        for v in sorted(a.vlans, key=lambda v: v.vlan_id):
            t.add([a.name, v.vlan_id, v.name,
                   v.isid if v.isid is not None else "",
                   v.isid_name, ",".join(v.members) or "-", len(v.members)])
    return t


def build_ports(audits: list[SwitchAudit]) -> Table:
    t = Table("Ports", ["Switch", "Port", "Description", "Admin", "Oper",
                        "Reason", "LLDP neighbor", "LLDP IP", "LLDP SysDescr",
                        "Uplink"])
    for a in audits:
        for p in a.ports:
            t.add([a.name, p.port, p.description,
                   _fmt_bool(p.admin_up, "enable", "disable"),
                   _fmt_bool(p.oper_up), p.state_reason, p.lldp_neighbor,
                   p.lldp_neighbor_ip, p.lldp_sys_descr,
                   "yes" if p.is_uplink else ""],
                  "warn" if p.is_uplink and not p.oper_up else None)
    return t


def build_mlts(audits: list[SwitchAudit]) -> Table:
    t = Table("MLTs", ["Switch", "MLT", "Name", "Type", "Admin", "Members",
                       "Members up", "IST", "Uplink"])
    for a in audits:
        for m in a.mlts:
            if not m.members:
                # dead MLT: nothing to recreate on the new switch
                t.add([a.name, m.mlt_id, m.name, m.mlt_type, m.admin,
                       "DEAD - no members", "0/0",
                       "yes" if m.is_ist else "", ""], "warn")
                continue
            up_display = m.members_up_display
            if m.members_up is None and m.in_datapath is False:
                up_display += " (datapath: NONE)"
            elif m.members_up is None and m.in_datapath is True:
                up_display += " (datapath: fwd)"
            t.add([a.name, m.mlt_id, m.name, m.mlt_type, m.admin,
                   ",".join(m.members),
                   up_display,
                   "yes" if m.is_ist else "",
                   "yes" if m.is_uplink else ""], _mlt_up_severity(m))
    return t


def build_vlan_comparison(comparisons: dict[str, list[VlanComparison]]) -> Table:
    t = Table("VLAN vs Fabric", ["Switch", "VLAN", "Name", "Local I-SID",
                                 "I-SID name", "Expected I-SID(s)",
                                 "DvR attached I-SID(s)", "Matched I-SID",
                                 "Status", "Detail"])
    for comps in comparisons.values():
        for c in comps:
            # EXCLUDED rows stay in the table (uncolored) so it is visible on
            # WHICH switches an intentionally-unfabriced VLAN exists
            t.add([c.switch, c.vlan_id, c.vlan_name,
                   c.local_isid if c.local_isid is not None else "",
                   c.vlan_isid_name,
                   ",".join(map(str, c.expected_isids)),
                   ",".join(map(str, c.dvr_isids)) or "-",
                   c.matched_isid if c.matched_isid is not None else "-",
                   c.status.value, c.detail],
                  None if c.status is CompStatus.EXCLUDED else c.severity)
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


def build_coverage(audits: list[SwitchAudit]) -> Table:
    """How complete the collected data actually is, per switch.

    Several sources are optional: a release that rejects `show vlan members`,
    or a run without the running-config, still produces a full-looking sheet -
    with thinner VLAN columns. That difference used to be invisible, which is
    how a cabling sheet reached a data centre missing VLANs nobody knew were
    missing. This sheet makes it a number you can look at before the window.
    """
    t = Table("Coverage", [
        "Switch", "Reachable", "Ports", "Ports with VLANs", "VLAN coverage",
        "Ports with I-SIDs", "Ports with tagging", "VLANs", "MLTs",
        "MLTs with VLANs", "Sources that answered", "Sources that did not",
    ])
    t.wrap_columns = ["Sources that answered", "Sources that did not"]
    for audit in sorted(audits, key=lambda a: a.name):
        total = len(audit.ports)
        with_vlans = audit.ports_with_vlans
        with_isids = sum(1 for p in audit.ports
                         if any(b.isid is not None for b in p.bindings))
        with_tagging = sum(1 for p in audit.ports
                           if any(b.tagging for b in p.bindings))
        mlts_with_vlans = sum(1 for m in audit.mlts if m.bindings)
        ok = [s.name for s in audit.sources if s.ok]
        missing = [f"{s.name} ({s.detail})" if s.detail else s.name
                   for s in audit.sources if not s.ok]
        pct = f"{with_vlans * 100 // total}%" if total else "-"
        # Severities here feed the process exit code, so 'error' has to mean a
        # collection that failed - not a switch with spare ports. A 48-port box
        # with 12 patched is normal and says nothing about coverage; a
        # reachable switch with ports and NO VLAN data at all is a real gap.
        severity = "ok"
        if not audit.reachable:
            severity = "error"
        elif total and not with_vlans:
            severity = "error"
        elif missing or (total and with_vlans < total) or not with_tagging:
            severity = "warn"
        t.add([
            audit.name, "yes" if audit.reachable else "NO", total, with_vlans,
            pct, with_isids, with_tagging, len(audit.vlans), len(audit.mlts),
            mlts_with_vlans, ", ".join(ok) or "-", ", ".join(missing) or "-",
        ], severity)
    return t


def build_all(audits: list[SwitchAudit], fabric: FabricState,
              comparisons: dict[str, list[VlanComparison]],
              no_fabric: bool = False) -> list[Table]:
    if no_fabric:
        # inventory/state only: no fabric collected, nothing to compare against
        return [
            build_summary(audits, comparisons, no_fabric=True),
            build_vlan_inventory(audits),
            build_ports(audits),
            build_mlts(audits),
            build_coverage(audits),
            build_issues(audits, fabric, comparisons),
        ]
    return [
        build_summary(audits, comparisons),
        build_vlan_comparison(comparisons),
        build_ports(audits),
        build_mlts(audits),
        build_fabric(fabric),
        build_coverage(audits),
        build_issues(audits, fabric, comparisons),
    ]
