"""Collect the migration-relevant state from one legacy switch (VOSS or ERS)."""

from __future__ import annotations

import fnmatch
import logging

from switch_migrator.config import Config, SwitchTarget
from switch_migrator.connection import BaseRunner, CommandError
from switch_migrator.models import Platform, SwitchAudit, VlanInfo
from switch_migrator.parsers import ers_parsers, voss_parsers
from switch_migrator.parsers.common import (
    PORT_RE,
    parse_lldp_neighbors,
    parse_lldp_neighbors_summary,
    parse_mac_table,
)

# how many learned MACs are kept per port for the migration sheet
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
        out = _run(audit, runner, command, required=False)
        if out:
            audit.ports = parser(out)
            if audit.ports:
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


def _enrich_migration_fields(audit: SwitchAudit, runner: BaseRunner,
                             pull_macs: bool = False) -> None:
    """Per-port data the migration sheets need: MLT membership + LACP, VLANs and
    their I-SIDs, tagging, and - when pull_macs is set - learned MACs and the
    pluggable optic. Everything except the MAC/optics fetch is derived from data
    already collected, so it costs no extra commands.
    """
    by_port = {p.port: p for p in audit.ports}

    # --- MLT membership (+ LACP, parsed earlier from the same show mlt output)
    for mlt in audit.mlts:
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
    for row in audit.port_isid_rows:
        port = by_port.get(row["port"])
        if port is None:
            continue
        if row["vlan"] is not None and row["vlan"] not in port.vlans:
            port.vlans.append(row["vlan"])
        if row["isid"] not in port.isids:
            port.isids.append(row["isid"])
    for port in audit.ports:
        port.vlans.sort()
        port.isids.sort()
        # a port carrying several VLANs must be tagged; a single VLAN is
        # normally the untagged/native one. Left blank when unknown.
        if len(port.vlans) > 1:
            port.tagging = "tagged"
        elif len(port.vlans) == 1:
            port.tagging = "untagged"

    if not pull_macs:
        return

    # --- learned MACs (capped per port; MLT entries fan out to their members)
    out = _run(audit, runner, "show mac-address-table", required=False, absent_ok=True)
    if out:
        table = parse_mac_table(out)
        by_mlt = {m.mlt_id: m for m in audit.mlts}
        for key, entries in table.items():
            targets: list = []
            if key.startswith("mlt:"):
                mlt = by_mlt.get(int(key.split(":", 1)[1]))
                targets = [by_port[m] for m in (mlt.members if mlt else [])
                           if m in by_port]
            elif key in by_port:
                targets = [by_port[key]]
            for port in targets:
                for mac, _vlan in entries:
                    port.mac_total += 1
                    if len(port.macs) < MAC_CAP and mac not in port.macs:
                        port.macs.append(mac)

    # --- pluggable optics (VOSS); the media type itself is already in the
    # port DESCRIPTION column, this adds the vendor/part when available
    if audit.platform is Platform.VOSS:
        out = _run(audit, runner, "show pluggable-optical-modules basic",
                   required=False, absent_ok=True)
        if out:
            for line in out.splitlines():
                tokens = line.split()
                if len(tokens) >= 2 and PORT_RE.match(tokens[0]) and "/" in tokens[0]:
                    port = by_port.get(tokens[0])
                    if port is not None and not port.transceiver:
                        port.transceiver = " ".join(tokens[1:4])[:40]


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

    _enrich_migration_fields(audit, runner, pull_macs=pull_macs)

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
