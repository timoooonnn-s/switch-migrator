"""Migration-day deliverables: the per-device info sheet and the DC cabling
sheet, plus ready-to-paste commands for the migration window.

Both sheets are keyed by a sequential migration port ID (P0001, ...) assigned
across the whole run - the same physical link keeps one ID even though several
old switches consolidate onto fewer new ones.
"""

from __future__ import annotations

from switch_migrator import location, usage
from switch_migrator.config_extract import extract_voss_config
from switch_migrator.location import LocationRules
from switch_migrator.models import (
    BOTH,
    UNTAGGED,
    Platform,
    PortState,
    SwitchAudit,
    tagging_summary,
)
from switch_migrator.parsers.common import port_sort_key
from switch_migrator.plan import OVERFLOW, MigrationPlan
from switch_migrator.report.tables import Table


def new_switch_names(value: str) -> list[str]:
    """The NEW switches this window targets, from the --new-switch value.

    One name is the common case; several, comma-separated, is a consolidation
    onto more than one box. The list becomes the dropdown on the sheet's NEW
    switch column, so a technician picks a target instead of typing one.
    """
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def assign_port_uids(audits: list[SwitchAudit], prefix: str = "P") -> None:
    """Assign stable sequential migration IDs across all audited switches.

    Deterministic: switches in name order, ports in faceplate order, so a
    re-run of the same inventory produces the same IDs and they ascend along
    the front of the switch.
    """
    n = 0
    for audit in sorted(audits, key=lambda a: a.name):
        for port in sorted(audit.ports, key=lambda x: port_sort_key(x.port)):
            n += 1
            port.uid = f"{prefix}{n:04d}"


def _connected(p: PortState) -> bool:
    """A port the DC techs actually have to re-patch.

    Not just 'link is up right now': a momentarily-down member of a still
    forwarding MLT, or a port that has passed traffic but is down at this
    instant, is very much still cabled. When the usage classification ran it is
    authoritative; otherwise fall back to the live signals.
    """
    if p.usage:
        return p.usage not in (usage.LIKELY_UNUSED, usage.UNUSED)
    return bool(p.oper_up or p.lldp_neighbor or p.lldp_neighbor_ip or p.macs)


def _usage_rank(p: PortState) -> int:
    """Sort key: in-use first, dead ports last."""
    return usage.ORDER.get(p.usage, 2)


def _macs_cell(p: PortState) -> str:
    if not p.macs:
        return ""
    text = ",".join(p.macs)
    extra = p.mac_total - len(p.macs)
    return text + (f" (+{extra} more)" if extra > 0 else "")


def _neighbor(p: PortState) -> str:
    """Best available identity of the device on the port."""
    return p.lldp_neighbor or p.lldp_neighbor_ip or p.lldp_sys_descr or ""


def _fmt_bool(v: bool | None, true: str, false: str) -> str:
    return "?" if v is None else (true if v else false)


def _pairs_cell(bindings) -> str:
    """The VLAN<->I-SID pairs of one port or MLT, as one self-contained cell.

    Untagged first, because that is the one a technician looking at an access
    port cares about, then by VLAN id. Every binding is printed - including the
    ones whose I-SID could not be resolved, which show '?' rather than being
    dropped, because a missing I-SID is a task and a blank cell is a trap.
    """
    ordered = sorted(bindings, key=lambda b: (
        0 if b.tagging in (UNTAGGED, BOTH) else 1, b.vlan is None, b.vlan or 0))
    return ", ".join(b.render() for b in ordered)


def _untagged_vlan(port: PortState) -> int | str:
    """The port's untagged (native/access) VLAN, or '' when it has none.

    Replaces the old leading column, which printed the numerically lowest VLAN
    on the port - meaningless on a trunk. This one is only ever filled in when
    a source actually said the VLAN egresses untagged.
    """
    for binding in port.bindings:
        if binding.tagging in (UNTAGGED, BOTH) and binding.vlan is not None:
            return binding.vlan
    return ""


def _tagging(port: PortState) -> str:
    """The port's tagging, from the bindings the same row prints.

    Falls back to the stored field for a port that has no bindings at all, so
    data restored from an older snapshot still shows what it knew.
    """
    return tagging_summary(port.bindings) or port.tagging


def _sources_cell(holder) -> str:
    """Where this row's VLAN/I-SID cell came from.

    Provenance matters because an empty cell has two very different meanings -
    'the port carries nothing' and 'nothing we could read said what it
    carries'. The source list tells them apart at a glance.
    """
    return holder.binding_sources or ("no VLAN source" if not holder.bindings else "")


def build_port_info(audits: list[SwitchAudit]) -> Table:
    """Sheet 1: every port with everything needed during the migration."""
    t = Table("Port Info", [
        "Port ID", "Switch", "Port", "Port name", "Device on port", "Neighbor IP",
        "MAC addresses", "Untagged VLAN", "Tagging", "VLAN -> I-SID",
        "VLAN IDs", "I-SIDs", "Admin", "Oper",
        "LACP", "MLT ID", "MLT name", "Transceiver", "Media", "Uplink",
        "Usage", "Why", "VLAN source",
    ])
    t.console_columns = ["Port ID", "Switch", "Port", "Device on port",
                         "VLAN -> I-SID", "Oper", "MLT ID", "Usage"]
    t.wrap_columns = ["VLAN -> I-SID", "MAC addresses", "Why"]
    for audit in sorted(audits, key=lambda a: a.name):
        for p in sorted(audit.ports, key=lambda x: port_sort_key(x.port)):
            t.add([
                p.uid, audit.name, p.port, p.name, _neighbor(p), p.lldp_neighbor_ip,
                _macs_cell(p), _untagged_vlan(p), _tagging(p), _pairs_cell(p.bindings),
                ",".join(map(str, p.vlans)), ",".join(map(str, p.isids)),
                _fmt_bool(p.admin_up, "enable", "disable"),
                _fmt_bool(p.oper_up, "up", "down"),
                _fmt_bool(p.lacp, "yes", "no"),
                p.mlt_id if p.mlt_id is not None else "",
                p.mlt_name, p.transceiver, p.media,
                "yes" if p.is_uplink else "",
                p.usage, p.usage_evidence, _sources_cell(p),
            ], "warn" if (p.usage == usage.DEGRADED
                          or (p.is_uplink and not p.oper_up)) else None)
    return t


def build_cabling_by_location(audits: list[SwitchAudit],
                              rules: LocationRules,
                              new_switches: list[str] | None = None,
                              plan: MigrationPlan | None = None) -> list[Table]:
    """One cabling worksheet per location group.

    Deliberately NOT accompanied by a combined sheet. This is a document people
    write into by hand: if the same link appeared on both a per-site tab and an
    all-sites tab, two technicians could fill in two copies of the same row and
    one set of answers would be lost. Each link belongs to exactly one sheet.

    The split keys on the OLD switch name, which is the only thing that exists
    when the sheet is written - the NEW columns are what gets filled in.
    """
    groups = location.split([a.name for a in audits], rules)
    by_name = {a.name: a for a in audits}
    tables = []
    for group, names in groups.items():
        table = build_cabling([by_name[n] for n in names], new_switches, plan)
        table.title = f"Cabling {group}"
        tables.append(table)
    return tables


def build_cabling(audits: list[SwitchAudit],
                  new_switches: list[str] | None = None,
                  plan: MigrationPlan | None = None) -> Table:
    """Sheet 2: the DC cabling worksheet - connected ports only, with empty
    columns the technicians fill in as they re-patch.

    With a `plan`, the NEW switch / port / MLT columns come pre-filled with the
    plan's proposal and 'Plan note' says which rule placed each link. They stay
    editable: what a human changes is read back as a pin next time. 'Done' is
    ticked when the cable has actually moved, so a verification mid-window
    checks the moved rows and leaves the planned ones pending.

    Laid out in the order the work actually happens: what the link is, where it
    is now (rack, switch, port), where it goes (rack, switch, port), and only
    then the detail needed to configure it. The rack columns are deliberately
    empty - no switch knows which rack it is in, so a human writes it once and
    every later row for that switch is easy to find on the floor.

    Deliberately WIDE ("one big paper"): every row is self-contained, carrying
    both the port's own VLAN<->I-SID pairs and, when the port is an MLT member,
    the MLT's id, name and pairs - so nobody has to cross-reference a second
    sheet while standing at the rack.

    Deliberately NOT accompanied by a combined sheet when split by location:
    this is a document people write into by hand, and if the same link appeared
    twice, one set of answers would be lost.
    """
    t = Table("Cabling", [
        "Port ID", "Usage", "Type", "Untagged VLAN", "End device / neighbor",
        "Port name",
        # where the link is today
        "Rack (old)", "Old switch", "Old port",
        # filled in by the technician / planner during the migration
        "Rack (new)", "NEW switch", "NEW port", "NEW MLT ID", "NEW MLT name",
        "NEW VLAN", "Done", "Plan note",
        # what has to end up configured on the new port
        "Tagging", "VLAN -> I-SID",
        "MLT ID", "MLT name", "LACP", "MLT VLAN -> I-SID",
        "Port VLANs", "Port I-SIDs", "MLT VLANs", "MLT I-SIDs",
        "MAC addresses", "Media", "Why", "VLAN source",
    ])
    t.console_columns = ["Port ID", "Usage", "Type", "Untagged VLAN",
                         "End device / neighbor", "Old switch", "Old port",
                         "MLT ID"]
    t.manual_columns = ["Rack (old)", "Rack (new)", "NEW switch", "NEW port",
                        "NEW MLT ID", "NEW MLT name", "NEW VLAN", "Done"]
    t.int_columns = ["NEW MLT ID", "NEW VLAN"]
    t.wrap_columns = ["VLAN -> I-SID", "MLT VLAN -> I-SID", "MAC addresses",
                      "Why", "End device / neighbor"]
    t.page_break_column = "Old switch"
    # Port ID, Usage, Type and the neighbour stay visible while the technician
    # scrolls right into the columns they are filling in
    t.freeze_columns = 6
    if new_switches:
        t.choice_columns = {"NEW switch": sorted(set(new_switches))}

    for audit in sorted(audits, key=lambda a: a.name):
        mlt_by_id = {m.mlt_id: m for m in audit.mlts}
        # in-use ports first so the techs work top-down; likely-dead ports stay
        # on the sheet (never silently dropped) but sort to the bottom
        for p in sorted(audit.ports,
                        key=lambda x: (_usage_rank(x), port_sort_key(x.port))):
            if not _connected(p) and not p.usage:
                continue
            kind = "uplink" if p.is_uplink else ("mlt" if p.mlt_id is not None
                                                 else "access")
            mlt = mlt_by_id.get(p.mlt_id) if p.mlt_id is not None else None
            move = plan.move_for(audit.name, p.port) if plan else None
            new = (["", move.new_switch, move.new_port,
                    move.new_mlt_id if move.new_mlt_id is not None else "",
                    move.new_mlt_name, "", "", move.note] if move
                   else ["", "", "", "", "", "", "", _not_moved_note(plan, audit.name, p.port)])
            t.add([
                p.uid, p.usage, kind, _untagged_vlan(p), _neighbor(p), p.name,
                "", audit.name, p.port,             # Rack (old) is filled in by hand
                *new,                               # rack/switch/port/MLT/VLAN/done/note
                _tagging(p), _pairs_cell(p.bindings),
                p.mlt_id if p.mlt_id is not None else "",
                p.mlt_name,
                # carried to --generate-mlt, which must never invent LACP
                _fmt_bool(p.lacp, "yes", "no") if p.mlt_id is not None else "",
                _pairs_cell(mlt.bindings) if mlt else "",
                ",".join(map(str, p.vlans)), ",".join(map(str, p.isids)),
                ",".join(map(str, mlt.vlans)) if mlt else "",
                ",".join(map(str, mlt.isids)) if mlt else "",
                _macs_cell(p), p.media, p.usage_evidence, _sources_cell(p),
            ], "warn" if p.usage == usage.DEGRADED else None)
    return t


def _not_moved_note(plan: MigrationPlan | None, switch: str, port: str) -> str:
    if plan is None:
        return ""
    for item in plan.not_moved:
        if item.link.key == (switch, port):
            return f"not planned: {item.reason}"
    return ""


def build_plan_tables(plan: MigrationPlan) -> list[Table]:
    """The plan at a glance: how full each new switch is, what does not move
    and why, and what a human should look at before the sheet goes out."""
    fill = Table("Plan", ["New switch", "Peer", "Access ports", "Dual-homed (from 1)",
                          "Single-homed (from the top)", "Pinned", "Free"])
    for f in plan.fill:
        fill.add([f.name, f.peer, f.access_ports, f.dual, f.single, f.pinned, f.free],
                 "error" if f.free < 0 else ("warn" if f.free == 0 else None))
    # 'Switch'/'Port', not 'Old ...': this is not a fill-in sheet and must never
    # be read back as one
    not_moved = Table("Not moved", ["Port ID", "Switch", "Port", "On it", "Why"])
    not_moved.wrap_columns = ["Why"]
    for item in sorted(plan.not_moved, key=lambda n: (n.link.old_switch,
                                                      port_sort_key(n.link.old_port))):
        not_moved.add([item.link.uid, item.link.old_switch, item.link.old_port,
                       item.link.neighbor, item.reason],
                      "error" if item.reason.startswith(OVERFLOW) else None)
    notes = Table("Plan notes", ["Kind", "Note"])
    notes.wrap_columns = ["Note"]
    for problem in plan.problems:
        notes.add(["problem", problem], "warn")
    for hint in plan.hints:
        notes.add(["hint", hint])
    for move in plan.moves:
        for flag in [*move.link.flags, *move.flags]:
            notes.add(["check", f"{move.link.old_switch} {move.link.old_port} -> "
                                f"{move.new_switch} {move.new_port}: {flag}"])
    return [fill, not_moved, notes]


def build_commands(audits: list[SwitchAudit], new_switch: str = "") -> str:
    """Ready-to-paste commands for the migration window: per-port MAC checks to
    run on the NEW switch, plus the neutralized config for each old device."""
    target = new_switch or "<new-switch>"
    out: list[str] = [
        "=" * 78,
        "MIGRATION COMMANDS - review before use",
        "=" * 78,
        "",
        f"### 1) MAC verification - run on the NEW switch ({target})",
        "#    After re-patching, check that the expected MACs reappear on the",
        "#    new port. 'Old port' is where the link used to sit; substitute the",
        "#    new port number once the cabling sheet is filled in.",
        "",
    ]
    for audit in sorted(audits, key=lambda a: a.name):
        connected = [p for p in audit.ports if _connected(p)]
        if not connected:
            continue
        out.append(f"# --- from {audit.name} ---")
        for p in connected:
            expect = _macs_cell(p) or "(no MACs learned)"
            out.append(f"# {p.uid}  old {audit.name} {p.port}  expect: {expect}")
            out.append(f"show interfaces gigabitEthernet fdb-entry <NEW-PORT>   "
                       f"# was {audit.name} {p.port}")
        out.append("")

    out += [
        "",
        f"### 2) Port state overview on the NEW switch ({target})",
        "show interfaces gigabitEthernet state",
        "show lldp neighbor summary",
        "show mlt",
        "show vlan i-sid",
        "",
        "### 3) Config to apply (neutralized from the old devices)",
        "#    VOSS sources are filtered/neutralized; review before pasting.",
        "",
    ]
    for audit in sorted(audits, key=lambda a: a.name):
        if audit.platform is Platform.VOSS and audit.running_config:
            out.append(f"# ===== {audit.name} =====")
            out.append(extract_voss_config(audit.running_config,
                                           device_name=audit.name).text)
        elif audit.running_config:
            out.append(f"# ===== {audit.name} (ERS) =====")
            out.append(f"# see config/{audit.name}.cfg - generated VOSS "
                       f"flex-UNI draft + I-SID decision worksheet")
    if not any(a.running_config for a in audits):
        out.append("# (run with --extract-config to include the device config here)")
    return "\n".join(out).rstrip() + "\n"
