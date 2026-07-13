"""Collect the migration-relevant state from one legacy switch (VOSS or ERS)."""

from __future__ import annotations

import fnmatch
import logging

from switch_migrator.config import Config, SwitchTarget
from switch_migrator.connection import BaseRunner, CommandError
from switch_migrator.models import Platform, SwitchAudit
from switch_migrator.parsers import ers_parsers, voss_parsers
from switch_migrator.parsers.common import parse_lldp_neighbors

log = logging.getLogger(__name__)


def _is_core_neighbor(sysname: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(sysname.lower(), p.lower()) for p in patterns)


def collect_switch(target: SwitchTarget, runner: BaseRunner, cfg: Config) -> SwitchAudit:
    audit = SwitchAudit(name=target.name, host=target.host,
                        platform=target.platform, reachable=True)
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
        msg = f"'{command}' failed: {str(exc.output).strip().splitlines()[0][:120] if exc.output else exc}"
        (audit.errors if required else audit.warnings).append(msg)
        log.log(logging.ERROR if required else logging.INFO, "[%s] %s", audit.name, msg)
        return None


def _collect_voss(audit: SwitchAudit, runner: BaseRunner) -> None:
    out = _run(audit, runner, "show interfaces gigabitEthernet interface", required=True)
    if out:
        audit.ports = voss_parsers.parse_ports(out)
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

    port_oper = {}
    for port in audit.ports:
        port.lldp_neighbor = neighbors.get(port.port, "")
        port.is_uplink = bool(port.lldp_neighbor) and _is_core_neighbor(
            port.lldp_neighbor, cfg.core_switch_patterns)
        port_oper[port.port] = bool(port.oper_up)

    for mlt in audit.mlts:
        mlt.members_up = sum(1 for m in mlt.members if port_oper.get(m, False))
        mlt.is_uplink = any(
            p.is_uplink for p in audit.ports if p.port in mlt.members)

    if audit.ist is not None and audit.ist.session_up is False:
        audit.warnings.append(
            f"IST/vIST session is DOWN (peer {audit.ist.peer_ip or 'unknown'})")
    for mlt in audit.mlts:
        if mlt.members and mlt.members_up < mlt.members_total:
            audit.warnings.append(
                f"MLT {mlt.mlt_id} ({mlt.name or 'unnamed'}): only "
                f"{mlt.members_up}/{mlt.members_total} member ports up")
