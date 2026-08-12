"""Migration-day deliverables: the per-device info sheet and the DC cabling
sheet, plus ready-to-paste commands for the migration window.

Both sheets are keyed by a sequential migration port ID (P0001, ...) assigned
across the whole run - the same physical link keeps one ID even though several
old switches consolidate onto fewer new ones.
"""

from __future__ import annotations

from switch_migrator.config_extract import extract_voss_config
from switch_migrator.models import Platform, PortState, SwitchAudit
from switch_migrator.report.tables import Table


def assign_port_uids(audits: list[SwitchAudit], prefix: str = "P") -> None:
    """Assign stable sequential migration IDs across all audited switches.

    Deterministic: switches in name order, ports in their collected order, so a
    re-run of the same inventory produces the same IDs.
    """
    n = 0
    for audit in sorted(audits, key=lambda a: a.name):
        for port in audit.ports:
            n += 1
            port.uid = f"{prefix}{n:04d}"


def _connected(p: PortState) -> bool:
    """A port the DC techs actually have to re-patch."""
    return bool(p.oper_up or p.lldp_neighbor or p.lldp_neighbor_ip or p.macs)


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


def build_port_info(audits: list[SwitchAudit]) -> Table:
    """Sheet 1: every port with everything needed during the migration."""
    t = Table("Port Info", [
        "Port ID", "Switch", "Port", "Device on port", "Neighbor IP",
        "MAC addresses", "Tagging", "VLAN IDs", "I-SIDs", "Admin", "Oper",
        "LACP", "MLT ID", "MLT name", "Transceiver", "Media", "Uplink",
    ])
    for audit in sorted(audits, key=lambda a: a.name):
        for p in audit.ports:
            t.add([
                p.uid, audit.name, p.port, _neighbor(p), p.lldp_neighbor_ip,
                _macs_cell(p), p.tagging,
                ",".join(map(str, p.vlans)), ",".join(map(str, p.isids)),
                _fmt_bool(p.admin_up, "enable", "disable"),
                _fmt_bool(p.oper_up, "up", "down"),
                _fmt_bool(p.lacp, "yes", "no"),
                p.mlt_id if p.mlt_id is not None else "",
                p.mlt_name, p.transceiver, p.media,
                "yes" if p.is_uplink else "",
            ], "warn" if p.is_uplink and not p.oper_up else None)
    return t


def build_cabling(audits: list[SwitchAudit]) -> Table:
    """Sheet 2: the DC cabling worksheet - connected ports only, with empty
    columns the technicians fill in as they re-patch.

    Deliberately WIDE ("one big paper"): every row is self-contained, carrying
    both the port's own VLANs/I-SIDs and, when the port is an MLT member, the
    MLT's id, name and its VLANs/I-SIDs - so nobody has to cross-reference a
    second sheet while standing at the rack.
    """
    t = Table("Cabling", [
        "VLAN", "Type", "Port ID", "End device / neighbor",
        "NEW switch", "NEW port",          # filled in by the technician
        "Old switch", "Old port",
        "MLT ID", "MLT name", "MLT VLANs", "MLT I-SIDs",
        "Port VLANs", "Port I-SIDs",
        "MAC addresses", "Media",
    ])
    for audit in sorted(audits, key=lambda a: a.name):
        mlt_by_id = {m.mlt_id: m for m in audit.mlts}
        for p in audit.ports:
            if not _connected(p):
                continue
            first_vlan = p.vlans[0] if p.vlans else ""
            kind = "uplink" if p.is_uplink else ("mlt" if p.mlt_id is not None
                                                 else "access")
            mlt = mlt_by_id.get(p.mlt_id) if p.mlt_id is not None else None
            # an MLT member's traffic is the union of the MLT's VLANs; fall back
            # to the MLT's list when the port itself has no per-port bindings
            if not first_vlan and mlt is not None and mlt.vlans:
                first_vlan = mlt.vlans[0]
            t.add([
                first_vlan, kind, p.uid, _neighbor(p),
                "", "",                     # NEW switch / NEW port: to fill in
                audit.name, p.port,
                p.mlt_id if p.mlt_id is not None else "",
                p.mlt_name,
                ",".join(map(str, mlt.vlans)) if mlt else "",
                ",".join(map(str, mlt.isids)) if mlt else "",
                ",".join(map(str, p.vlans)), ",".join(map(str, p.isids)),
                _macs_cell(p), p.media,
            ])
    return t


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
