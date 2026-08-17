"""Collect the migration-relevant state from one legacy switch (VOSS or ERS)."""

from __future__ import annotations

import fnmatch
import logging

from switch_migrator.config import Config, SwitchTarget
from switch_migrator.connection import BaseRunner, CommandError
from switch_migrator.models import Platform, PortState, SwitchAudit, VlanInfo
from switch_migrator.parsers import ers_parsers, voss_parsers
from switch_migrator.usage import classify_audit, parse_uptime_days
from switch_migrator.parsers.common import (
    PORT_RE,
    parse_lldp_neighbors,
    parse_lldp_neighbors_summary,
    parse_mac_table,
)

# fallback cap on learned MACs kept per port; the configured value
# (Config.mac_cap) is what the collection actually uses
MAC_CAP = 10

log = logging.getLogger(__name__)


def _is_core_neighbor(sysname: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(sysname.lower(), p.lower()) for p in patterns)


def collect_switch(target: SwitchTarget, runner: BaseRunner, cfg: Config,
                   pull_config: bool = False,
                   pull_macs: bool = False) -> SwitchAudit:
    audit = SwitchAudit(name=target.name, host=target.host,
                        platform=target.platform, reachable=True)
    # session-setup problems (e.g. paging disable rejected) must be visible
    audit.warnings.extend(getattr(runner, "setup_warnings", []))
    if target.platform is Platform.VOSS:
        _collect_voss(audit, runner)
    else:
        _collect_ers(audit, runner)
    _enrich(audit, runner, cfg, pull_macs=pull_macs)
    # running-config is only pulled for the --extract-config feature (VOSS:
    # filter+neutralize; ERS: translate to VOSS flex-UNI)
    if pull_config:
        out = _run(audit, runner, "show running-config", required=False, absent_ok=True)
        audit.running_config = out or ""
    if pull_macs:
        # switch uptime tells us how long the interface counters have been
        # accumulating - '0 packets' only means something on a long-running box
        out = _run(audit, runner, "show sys-info", required=False, absent_ok=True)
        if out:
            audit.uptime_days = parse_uptime_days(out)
        classify_audit(audit, unused_after_days=cfg.unused_after_days,
                       uptime_days=audit.uptime_days)
    return audit


def _run(audit: SwitchAudit, runner: BaseRunner, command: str,
         required: bool, absent_ok: bool = False) -> str | None:
    try:
        return runner.run(command)
    except CommandError as exc:
        # condense the device's answer so the report shows WHY it failed
        detail = " | ".join(
            line.strip() for line in str(exc.output or exc).splitlines()
            if line.strip())[:200]
        # absent_ok: this command legitimately does not exist on every model /
        # release (e.g. 'show ist' on access ERS without an IST, the lldp
        # variant of the other platform, 'show vlan members' on older BOSS).
        # Its failure is expected version variance - log it, but keep it out of
        # the report entirely.
        if absent_ok and not required:
            log.info("[%s] '%s' unavailable on this device (%s) - skipped",
                     audit.name, command, detail)
            return None
        msg = f"'{command}' failed: {detail}"
        (audit.errors if required else audit.warnings).append(msg)
        log.log(logging.ERROR if required else logging.INFO, "[%s] %s", audit.name, msg)
        return None


def _merge_port_details(audit: SwitchAudit, extra: list[PortState]) -> None:
    """Fill gaps in the already-collected ports from a second port table.

    Only ever adds: the first source stays authoritative for the state it
    reported, and ports it never mentioned are appended rather than dropped.
    """
    by_port = {p.port: p for p in audit.ports}
    for other in extra:
        port = by_port.get(other.port)
        if port is None:
            audit.ports.append(other)
            continue
        port.description = port.description or other.description
        if port.admin_up is None:
            port.admin_up = other.admin_up
        if port.oper_up is None:
            port.oper_up = other.oper_up


def _collect_voss(audit: SwitchAudit, runner: BaseRunner) -> None:
    # Port-state fallback chain: which variants exist differs across 8.x
    # releases. First command that yields ports wins. The plain
    # `show interfaces gigabitEthernet` is deliberately NOT used: it prints
    # several full-width sections (Port Interface, Port Name, Port Config, ...)
    # per port - 2000+ lines on large stacks - which is slow and can desync the
    # session. Both variants below are one narrow row per port and carry the
    # ADMIN/OPER state we actually need.
    port_sources = (
        ("show interfaces gigabitEthernet state", voss_parsers.parse_port_state),
        ("show interfaces gigabitEthernet interface", voss_parsers.parse_ports),
    )
    for command, parser in port_sources:
        # once we already have the port state, a later variant only adds the
        # media type - its absence on this release is not worth a warning
        out = _run(audit, runner, command, required=False,
                   absent_ok=bool(audit.ports))
        if not out:
            continue
        parsed = parser(out)
        if not parsed:
            continue
        if not audit.ports:
            audit.ports = parsed
            # the 'state' table has no DESCRIPTION column, so on its own it
            # leaves the media type blank. Keep going to the 'interface'
            # variant and merge that column in rather than sending a second
            # command later; both tables are one narrow row per port.
            if all(p.description for p in parsed):
                break
            continue
        _merge_port_details(audit, parsed)
        break
    if not audit.ports:
        audit.errors.append(
            "no port state obtained: all 'show interfaces gigabitEthernet' "
            "variants failed or returned nothing")
    out = _run(audit, runner, "show mlt", required=True)
    if out:
        audit.mlts = voss_parsers.parse_mlt(out)
        # LACP admin state comes from the same output - parse it here rather
        # than sending 'show mlt' a second time later
        lacp = voss_parsers.parse_mlt_lacp(out)
        for mlt in audit.mlts:
            mlt.lacp = lacp.get(mlt.mlt_id)
    out = _run(audit, runner, "show virtual-ist", required=False)
    if out:
        audit.ist = voss_parsers.parse_virtual_ist(out)
    out = _run(audit, runner, "show vlan i-sid", required=True)
    if out:
        audit.vlans = voss_parsers.parse_vlan_isid(out)
    out = _run(audit, runner, "show vlan basic", required=False)
    names = voss_parsers.parse_vlan_basic(out) if out else {}
    for vlan in audit.vlans:
        # the VLAN's own name; if 'show vlan basic' didn't cover it, fall back
        # to the I-SID name so the name column is never needlessly blank
        vlan.name = vlan.name or names.get(vlan.vlan_id, "") or vlan.isid_name
    # configured port membership per VLAN (for the inventory report); optional
    # and quiet - not every release has it and it is never load-bearing
    out = _run(audit, runner, "show vlan members", required=False, absent_ok=True)
    if out:
        members = voss_parsers.parse_vlan_members(out)
        for vlan in audit.vlans:
            vlan.members = members.get(vlan.vlan_id, vlan.members)
    # port-level I-SID bindings catch services (e.g. CVLAN/switched-UNI) that
    # `show vlan i-sid` may not list
    out = _run(audit, runner, "show interfaces gigabitEthernet i-sid", required=False)
    if out:
        port_isid_rows = voss_parsers.parse_port_isid(out)
        # keep them for the per-port VLAN/I-SID columns (flex-UNI boxes have no
        # platform-VLAN members) instead of re-running the command later
        audit.port_isid_rows = port_isid_rows
        by_vlan = {v.vlan_id: v for v in audit.vlans}
        for row in port_isid_rows:
            if row["vlan"] is None:
                continue
            existing = by_vlan.get(row["vlan"])
            if existing is None:
                new = VlanInfo(vlan_id=row["vlan"], isid=row["isid"])
                audit.vlans.append(new)
                by_vlan[row["vlan"]] = new
            elif existing.isid is None:
                existing.isid = row["isid"]
            elif existing.isid != row["isid"]:
                audit.warnings.append(
                    f"VLAN {row['vlan']}: port {row['port']} binds I-SID "
                    f"{row['isid']} but the VLAN-level binding is {existing.isid}")


def _collect_ers(audit: SwitchAudit, runner: BaseRunner) -> None:
    out = _run(audit, runner, "show interfaces", required=True)
    if out:
        audit.ports = ers_parsers.parse_ports(out)
    out = _run(audit, runner, "show mlt", required=True)
    if out:
        audit.mlts = ers_parsers.parse_mlt(out)
    # access ERS boxes dual-homed to an SMLT core have no IST of their own and
    # reject the command outright - that is expected, not a finding
    out = _run(audit, runner, "show ist", required=False, absent_ok=True)
    if out:
        audit.ist = ers_parsers.parse_ist(out)
    out = _run(audit, runner, "show vlan", required=True)
    if out:
        audit.vlans = ers_parsers.parse_vlans(out)


def _collect_fdb(audit: SwitchAudit, runner: BaseRunner,
                 by_port: dict) -> dict[str, list[tuple[str, int | None]]]:
    """Learned MAC addresses per port.

    VOSS has no `show mac-address-table`; the forwarding database is read per
    port with `show interfaces gigabitEthernet fdb-entry [<port>]`. The bare
    form (whole box in one command) is tried first; if the release insists on a
    port argument, fall back to asking only the ports that are operationally UP
    - a down port has nothing learned, and this keeps a 48-port box from costing
    48 commands. ERS/BOSS keeps the classic `show mac-address-table`.
    """
    if audit.platform is not Platform.VOSS:
        out = _run(audit, runner, "show mac-address-table",
                   required=False, absent_ok=True)
        return parse_mac_table(out) if out else {}

    out = _run(audit, runner, "show interfaces gigabitEthernet fdb-entry",
               required=False, absent_ok=True)
    if out:
        table = parse_mac_table(out)
        if table:
            return table

    table: dict[str, list[tuple[str, int | None]]] = {}
    up_ports = [p.port for p in audit.ports if p.oper_up]
    if not up_ports:
        return table
    log.info("[%s] per-port FDB fallback for %d up port(s)",
             audit.name, len(up_ports))
    for port in up_ports:
        out = _run(audit, runner,
                   f"show interfaces gigabitEthernet fdb-entry {port}",
                   required=False, absent_ok=True)
        if out:
            for key, entries in parse_mac_table(out).items():
                table.setdefault(key, []).extend(entries)
    return table


def _enrich_migration_fields(audit: SwitchAudit, runner: BaseRunner,
                             pull_macs: bool = False,
                             mac_cap: int = MAC_CAP) -> None:
    """Per-port data the migration sheets need: MLT membership + LACP, VLANs and
    their I-SIDs, tagging, and - when pull_macs is set - learned MACs and the
    pluggable optic. Everything except the MAC/optics fetch is derived from data
    already collected, so it costs no extra commands.
    """
    by_port = {p.port: p for p in audit.ports}

    # --- MLT membership (+ LACP, parsed earlier from the same show mlt output)
    isid_of_vlan = {v.vlan_id: v.isid for v in audit.vlans}
    for mlt in audit.mlts:
        # the MLT's own VLAN IDS list (from show mlt) mapped to I-SIDs
        mlt.isids = sorted({isid_of_vlan[v] for v in mlt.vlans
                            if isid_of_vlan.get(v) is not None})
        for member in mlt.members:
            port = by_port.get(member)
            if port is None:
                continue
            port.mlt_id, port.mlt_name = mlt.mlt_id, mlt.name
            port.lacp = mlt.lacp

    # --- VLANs / I-SIDs per port (reverse of the VLAN member lists)
    isid_of = {v.vlan_id: v.isid for v in audit.vlans}
    for vlan in audit.vlans:
        for member in vlan.members:
            port = by_port.get(member)
            if port is None:
                continue
            if vlan.vlan_id not in port.vlans:
                port.vlans.append(vlan.vlan_id)
            isid = isid_of.get(vlan.vlan_id)
            if isid is not None and isid not in port.isids:
                port.isids.append(isid)
    # On a flex-UNI box the VLANs are not platform-VLAN members but per-port
    # I-SID bindings, so `show vlan members` yields nothing - use the port
    # bindings already collected as the second source (no extra command).
    tags_a_cvid: set[str] = set()
    for row in audit.port_isid_rows:
        port = by_port.get(row["port"])
        if port is None:
            continue
        if row["vlan"] is not None and row["vlan"] not in port.vlans:
            port.vlans.append(row["vlan"])
        if row["isid"] not in port.isids:
            port.isids.append(row["isid"])
        if row.get("cvid") is not None:
            tags_a_cvid.add(row["port"])
    for port in audit.ports:
        port.vlans.sort()
        port.isids.sort()
        if port.port in tags_a_cvid:
            # A c-vid IS the tag the host puts on the wire, so this is settled
            # rather than guessed - and it has to win over the count rule
            # below, which would call a flex-UNI port carrying exactly one
            # c-vid 'untagged' and have somebody configure the new port that
            # way.
            port.tagging = "tagged"
        elif len(port.vlans) > 1:
            # several VLANs on one port can only be a trunk
            port.tagging = "tagged"
        elif len(port.vlans) == 1:
            # traditional `vlan i-sid` model: one platform VLAN and nothing
            # that distinguishes tagged from untagged, so this stays the
            # inference it always was - usually the native VLAN
            port.tagging = "untagged"

    if not pull_macs:
        return

    # --- learned MACs (capped per port; MLT entries fan out to their members)
    table = _collect_fdb(audit, runner, by_port)
    if table:
        by_mlt = {m.mlt_id: m for m in audit.mlts}
        by_mlt_name = {m.name: m for m in audit.mlts if m.name}
        for key, entries in table.items():
            targets: list = []
            mlt = None
            if key.startswith("mlt:"):
                mlt = by_mlt.get(int(key.split(":", 1)[1]))
            elif key.startswith("name:"):
                # real VOSS prints the MLT's NAME in the fdb INTERFACE column
                mlt = by_mlt_name.get(key.split(":", 1)[1])
                if mlt is None:
                    log.info("[%s] fdb interface '%s' matches no known MLT - "
                             "entries skipped", audit.name, key[5:])
            if mlt is not None:
                targets = [by_port[m] for m in mlt.members if m in by_port]
            elif key in by_port:
                targets = [by_port[key]]
            for port in targets:
                for mac, _vlan in entries:
                    port.mac_total += 1
                    if len(port.macs) < mac_cap and mac not in port.macs:
                        port.macs.append(mac)

    # --- pluggable optics (VOSS). Real columns are
    #   PORT NUM | TYPE | DDM SUPPORTED | VENDOR NAME | PART NUMBER | SKU
    # so the TYPE/VENDOR/PART are taken around the TRUE/FALSE DDM token rather
    # than by blind position (releases add/drop trailing columns).
    if audit.platform is Platform.VOSS:
        out = _run(audit, runner, "show pluggable-optical-modules basic",
                   required=False, absent_ok=True)
        if out:
            for line in out.splitlines():
                tokens = line.split()
                if len(tokens) < 2 or not PORT_RE.match(tokens[0]) \
                        or "/" not in tokens[0]:
                    continue
                port = by_port.get(tokens[0])
                if port is None or port.transceiver:
                    continue
                ddm = next((i for i, t in enumerate(tokens)
                            if t.upper() in ("TRUE", "FALSE")), None)
                if ddm is not None:
                    parts = tokens[1:ddm] + tokens[ddm + 1:]   # drop the DDM flag
                else:
                    parts = tokens[1:]
                port.transceiver = " ".join(parts)[:60]

        # --- interface counters: has this port EVER passed traffic?
        # Deliberately layout-independent: the classification only needs
        # "are all counters on this row zero?", so no assumption is made about
        # which column is packets-in vs octets-out (that layout varies by
        # release and I have no capture to pin it to).
        out = _run(audit, runner, "show interfaces gigabitEthernet statistics",
                   required=False, absent_ok=True)
        if out:
            for line in out.splitlines():
                tokens = line.split()
                if len(tokens) < 2 or not PORT_RE.match(tokens[0]) \
                        or "/" not in tokens[0]:
                    continue
                port = by_port.get(tokens[0])
                if port is None:
                    continue
                counters = [int(t) for t in tokens[1:] if t.isdigit()]
                if counters:
                    port.has_traffic = any(c > 0 for c in counters)


def _enrich(audit: SwitchAudit, runner: BaseRunner, cfg: Config,
            pull_macs: bool = False) -> None:
    """LLDP neighbor names, uplink flags and MLT member-up counts."""
    # Platform-native command first; the other form only as fallback. VOSS has
    # the compact one-line-per-neighbor summary (preferred - stays small on
    # fully-cabled 48-port boxes); BOSS/ERS only knows the block form and
    # answers 'Invalid input' to 'summary'. absent_ok keeps the fallback dance
    # out of the report - only the aggregate warning below matters.
    if audit.platform is Platform.VOSS:
        lldp_sources = (
            ("show lldp neighbor summary", parse_lldp_neighbors_summary),
            ("show lldp neighbor", parse_lldp_neighbors),
        )
    else:
        lldp_sources = (
            ("show lldp neighbor", parse_lldp_neighbors),
            ("show lldp neighbor summary", parse_lldp_neighbors_summary),
        )
    neighbors: dict = {}
    for command, parser in lldp_sources:
        out = _run(audit, runner, command, required=False, absent_ok=True)
        if out:
            neighbors = parser(out)
            if neighbors:
                break
    if not neighbors:
        audit.warnings.append(
            "no LLDP neighbor data - uplink detection disabled for this switch")

    have_port_state = bool(audit.ports)
    port_oper = {}
    for port in audit.ports:
        n = neighbors.get(port.port)
        if n is not None:
            port.lldp_neighbor = n.sysname
            port.lldp_neighbor_ip = n.ip
            port.lldp_sys_descr = n.sys_descr
        # uplink detection keys on the advertised name (a hostname); adapter
        # models / empty names simply never match the core patterns
        port.is_uplink = bool(port.lldp_neighbor) and _is_core_neighbor(
            port.lldp_neighbor, cfg.core_switch_patterns)
        port_oper[port.port] = bool(port.oper_up)

    for mlt in audit.mlts:
        # Only a real port-state read gives a real up-count. With none, leave
        # members_up = None (unknown) instead of counting every member as down.
        mlt.members_up = (
            sum(1 for m in mlt.members if port_oper.get(m, False))
            if have_port_state else None)
        mlt.is_uplink = any(
            p.is_uplink for p in audit.ports if p.port in mlt.members)

    _enrich_migration_fields(audit, runner, pull_macs=pull_macs,
                             mac_cap=cfg.mac_cap)

    if audit.ist is not None and audit.ist.session_up is False:
        audit.warnings.append(
            f"IST/vIST session is DOWN (peer {audit.ist.peer_ip or 'unknown'})")
    for mlt in audit.mlts:
        label = f"MLT {mlt.mlt_id} ({mlt.name or 'unnamed'})"
        if not mlt.members:
            audit.warnings.append(
                f"{label}: DEAD MLT - no member ports left; does not need to be "
                f"recreated on the new switch")
        elif mlt.members_up is None:
            # No usable port state. Fall back to the data-path table: NONE means
            # the MLT is genuinely down; LOCAL/REMOTE means it is forwarding.
            # Never emit a bogus '0/N up' here.
            if mlt.in_datapath is False:
                audit.warnings.append(
                    f"{label}: not programmed in the data path - member ports "
                    f"{','.join(mlt.members)} appear DOWN (per-port state "
                    f"unavailable; inferred from 'show mlt')")
        elif mlt.members_up < mlt.members_total:
            audit.warnings.append(
                f"{label}: only {mlt.members_up}/{mlt.members_total} member ports up")
