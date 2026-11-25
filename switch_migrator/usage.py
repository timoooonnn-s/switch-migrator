"""Decide whether a port is actually IN USE, from evidence that carries history.

A single snapshot cannot answer "is this port used?" - link state is one
instant, and a momentarily-down MLT member looks exactly like a dead port. So
the classification combines three pieces of evidence, each of which says
something about TIME:

  * how long the port has been in its current state (the DATE column of
    `show interfaces gigabitEthernet state`),
  * whether it has ever passed traffic (interface counters, read together with
    the switch uptime so "0 packets" is only trusted on a long-running box),
  * whether it belongs to an MLT that is still forwarding - a down member of a
    live MLT is a FAULT, not an unused port.

Nothing is ever silently dropped: every port gets a class and the evidence
behind it, so a human can overrule the tool.
"""

from __future__ import annotations

import re
from datetime import datetime

from switch_migrator.models import MltState, PortState, SwitchAudit

IN_USE = "IN USE"
DEGRADED = "IN USE - degraded"
UNCERTAIN = "UNCERTAIN"
LIKELY_UNUSED = "LIKELY UNUSED"
UNUSED = "UNUSED"

# sort order for the reports: work top-down, dead ports last
ORDER = {IN_USE: 0, DEGRADED: 1, UNCERTAIN: 2, LIKELY_UNUSED: 3, UNUSED: 4}

_DATE_FORMATS = ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                 "%m/%d/%y", "%m/%d/%Y")


def parse_last_change_days(raw: str, now: datetime | None = None) -> int | None:
    """Age in days of a `MM/DD/YY HH:MM:SS` last-change stamp, or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    now = now or datetime.now()
    for fmt in _DATE_FORMATS:
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        delta = (now - when).days
        return delta if delta >= 0 else 0
    return None


def parse_uptime_days(sys_info: str) -> int | None:
    """Days from `SysUpTime : 135 day(s), 07:12:09`. Tells us how long the
    interface counters have been accumulating - '0 packets' on a box that
    rebooted an hour ago proves nothing."""
    m = re.search(r"SysUpTime\s*:\s*([\d,]+)\s*day", sys_info, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


# (class, the evidence that decided it - it leads the evidence text)
Verdict = tuple[str, list[str]]


def classify_port(port: PortState, mlt: MltState | None,
                  unused_after_days: int = 30,
                  uptime_days: int | None = None) -> tuple[str, str]:
    """Return (class, evidence) for one port."""
    ev: list[str] = []
    days = port.last_change_days
    if days is not None:
        ev.append(f"state unchanged {days}d")
    if port.has_traffic is True:
        ev.append("has traffic counters")
    elif port.has_traffic is False:
        ev.append("0 traffic counters"
                  + (f" over {uptime_days}d uptime" if uptime_days else ""))
    usage, lead = (_in_use(port) or _degraded(mlt)
                   or _down(port, unused_after_days, uptime_days))
    return usage, ", ".join([*lead, *ev])


def _in_use(port: PortState) -> Verdict | None:
    """Positively in use, in decreasing directness."""
    if port.oper_up:
        lead = ["link up"]
        if port.lldp_neighbor or port.lldp_neighbor_ip:
            lead.append(f"LLDP {port.lldp_neighbor or port.lldp_neighbor_ip}")
        if port.macs:
            lead.append(f"{port.mac_total or len(port.macs)} MAC(s)")
        return IN_USE, lead
    if port.macs:
        return IN_USE, [f"{port.mac_total or len(port.macs)} MAC(s) learned"]
    return None


def _degraded(mlt: MltState | None) -> Verdict | None:
    """Down, but part of an MLT that is still forwarding => a FAULT, and the
    cable is definitely still in use."""
    if mlt is None or not (mlt.in_datapath is True or bool(mlt.members_up)):
        return None
    up = (f"{mlt.members_up}/{mlt.members_total}"
          if mlt.members_up is not None else "?")
    return DEGRADED, [f"member of MLT {mlt.mlt_id} still forwarding ({up} up)"]


def _down(port: PortState, unused_after_days: int, uptime_days: int | None) -> Verdict:
    """Down: how confident are we that it is dead?"""
    days = port.last_change_days
    if port.has_traffic is True:
        return UNCERTAIN, ["link down but has passed traffic"]
    if days is None:
        return UNCERTAIN, ["link down, age of the change unknown"]
    if days < unused_after_days:
        return UNCERTAIN, [f"link down only {days}d (< {unused_after_days}d)"]
    # down long enough to call it. Counters make it certain; without them, or on
    # a recently-rebooted box, stay at LIKELY.
    counters_trustworthy = (port.has_traffic is False
                            and (uptime_days is None
                                 or uptime_days >= unused_after_days))
    usage = UNUSED if counters_trustworthy else LIKELY_UNUSED
    if port.admin_up is False:
        return usage, ["admin disabled"]
    return usage, [f"link down {days}d"]


def classify_audit(audit: SwitchAudit, unused_after_days: int = 30,
                   uptime_days: int | None = None,
                   now: datetime | None = None) -> None:
    """Fill in usage/usage_evidence for every port of one switch."""
    by_mlt = {m.mlt_id: m for m in audit.mlts}
    for port in audit.ports:
        if port.last_change_days is None:
            port.last_change_days = parse_last_change_days(port.last_change, now)
        mlt = by_mlt.get(port.mlt_id) if port.mlt_id is not None else None
        port.usage, port.usage_evidence = classify_port(
            port, mlt, unused_after_days=unused_after_days,
            uptime_days=uptime_days)
