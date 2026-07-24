"""Collect the migration-relevant state from one legacy switch (VOSS or ERS)."""

from __future__ import annotations

import fnmatch
import logging

from switch_migrator.config import Config, SwitchTarget
from switch_migrator.connection import BaseRunner, CommandError
from switch_migrator.models import Platform, SwitchAudit, VlanInfo
from switch_migrator.parsers import ers_parsers, voss_parsers
from switch_migrator.parsers.common import (
    parse_lldp_neighbors,
    parse_lldp_neighbors_summary,
)

log = logging.getLogger(__name__)


def _is_core_neighbor(sysname: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(sysname.lower(), p.lower()) for p in patterns)


def collect_switch(target: SwitchTarget, runner: BaseRunner, cfg: Config,
                   pull_config: bool = False) -> SwitchAudit:
    audit = SwitchAudit(name=target.name, host=target.host,
                        platform=target.platform, reachable=True)
    # session-setup problems (e.g. paging disable rejected) must be visible
    audit.warnings.extend(getattr(runner, "setup_warnings", []))
    if target.platform is Platform.VOSS:
        _collect_voss(audit, runner)
    else:
        _collect_ers(audit, runner)
    _enrich(audit, runner, cfg)
    # running-config is only pulled for the --extract-config feature; VOSS only
    # for now (the ERS generator is a later slice)
    if pull_config and target.platform is Platform.VOSS:
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
        by_vlan = {v.vlan_id: v for v in audit.vlans}
        for row in voss_parsers.parse_port_isid(out):
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


def _enrich(audit: SwitchAudit, runner: BaseRunner, cfg: Config) -> None:
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
