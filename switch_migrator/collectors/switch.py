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


def collect_switch(target: SwitchTarget, runner: BaseRunner, cfg: Config) -> SwitchAudit:
    audit = SwitchAudit(name=target.name, host=target.host,
                        platform=target.platform, reachable=True)
    # session-setup problems (e.g. paging disable rejected) must be visible
    audit.warnings.extend(getattr(runner, "setup_warnings", []))
    if target.platform is Platform.VOSS:
        _collect_voss(audit, runner)
    else:
        _collect_ers(audit, runner)
    _enrich(audit, runner, cfg)
    return audit


def _run(audit: SwitchAudit, runner: BaseRunner, command: str,
         required: bool) -> str | None:
    try:
        return runner.run(command)
    except CommandError as exc:
        # condense the device's answer so the report shows WHY it failed
        detail = " | ".join(
            line.strip() for line in str(exc.output or exc).splitlines()
            if line.strip())[:200]
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
    if out:
        names = voss_parsers.parse_vlan_basic(out)
        for vlan in audit.vlans:
            vlan.name = vlan.name or names.get(vlan.vlan_id, "")
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
    out = _run(audit, runner, "show ist", required=False)
    if out:
        audit.ist = ers_parsers.parse_ist(out)
    out = _run(audit, runner, "show vlan", required=True)
    if out:
        audit.vlans = ers_parsers.parse_vlans(out)


def _enrich(audit: SwitchAudit, runner: BaseRunner, cfg: Config) -> None:
    """LLDP neighbor names, uplink flags and MLT member-up counts."""
    neighbors: dict[str, str] = {}
    out = _run(audit, runner, "show lldp neighbor", required=False)
    if out:
        neighbors = parse_lldp_neighbors(out)
    if not neighbors:
        # some releases reject the block form; the summary table is a
        # separate command tree and often still available
        out = _run(audit, runner, "show lldp neighbor summary", required=False)
        if out:
            neighbors = parse_lldp_neighbors_summary(out)
    if not neighbors:
        audit.warnings.append(
            "no LLDP neighbor data - uplink detection disabled for this switch")

    have_port_state = bool(audit.ports)
    port_oper = {}
    for port in audit.ports:
        port.lldp_neighbor = neighbors.get(port.port, "")
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
