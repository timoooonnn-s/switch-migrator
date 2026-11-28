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


_SYS_DESCR = re.compile(r"^(?P<indent>\s*)sysDescr\s*:\s*(?P<descr>.+?)\s*$",
                        re.IGNORECASE | re.MULTILINE)
_MODEL_NAME = re.compile(r"^\s*ModelName\s*:\s*(?P<model>\S.*?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
# VOSS: 'SysDescr : 5520-24T-FabricEngine (9.4.0.0)'
_VOSS_RELEASE = re.compile(r"\((?P<release>\d+(?:\.\d+)+)\)\s*$")
# ERS/BOSS: 'SW:v5.6.1.053', on the sysDescr line or on the one below it
_BOSS_RELEASE = re.compile(r"\bSW:\s*v?(?P<release>\d+(?:\.\d+)+)", re.IGNORECASE)
# where the ERS version fields start: everything from here on is not the model
_BOSS_FIELDS = re.compile(r"\s+(?:HW|FW|SW):.*$", re.IGNORECASE)
# a continuation line may sit a column or two left of the value it continues
_CONTINUATION_SLACK = 2


def _sys_descr(sys_info: str) -> tuple[str, str]:
    """(first line, whole value) of sysDescr, continuation lines included.

    ERS/BOSS wraps it: a real 4548GT-PWR prints

        sysDescr:              Ethernet Routing Switch 4548GT-PWR
                               HW:12       FW:5.3.0.3   SW:v5.6.1.053
                               Mfg Date:20120121    HW Dev:

    so the release is NOT on the sysDescr line, and reading that line alone
    left the release of every such ERS empty. A continuation line is one
    indented about as far as the value itself; the next field's label sits
    further left (labels are right-aligned on some releases, left on others).
    """
    found = _SYS_DESCR.search(sys_info)
    if not found:
        return "", ""
    value_col = found.start("descr") - found.start()
    label_col = len(found["indent"])
    lines = [found["descr"]]
    for line in sys_info[found.end():].splitlines()[1:]:
        indent = len(line) - len(line.lstrip())
        if not line.strip() or indent <= label_col \
                or indent < value_col - _CONTINUATION_SLACK:
            break
        lines.append(line.strip())
    return lines[0], " ".join(lines)


def parse_release_and_model(sys_info: str) -> tuple[str, str]:
    """(release, model) from `show sys-info`, '' for whatever is not printed.

    Which release a switch runs is what scopes a parser problem: VOSS 9.x
    inserts table columns 8.x does not print, so 'which of these 300 boxes
    produced output this build reads differently' is a question the evidence
    has to be able to answer. VOSS puts the release in the parentheses of
    SysDescr (capture: tests/fixtures/voss_94); ERS/BOSS in a `SW:` field
    that may wrap onto the line below (see `_sys_descr`).
    """
    first, text = _sys_descr(sys_info)
    model = _MODEL_NAME.search(sys_info)
    found = _VOSS_RELEASE.search(first) or _BOSS_RELEASE.search(text)
    release = found["release"] if found else ""
    if model:
        return release, model["model"]
    # no ModelName (ERS/BOSS): the descr itself names the box, minus the
    # release in parentheses and the HW:/FW:/SW: fields - otherwise every
    # firmware level would count as a different model
    return release, _BOSS_FIELDS.sub("", _VOSS_RELEASE.sub("", first)).strip()


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
    # a box whose uptime we do not know, stay at LIKELY.
    #
    # An UNKNOWN uptime is not a long one. '0 packets' only means something
    # when the counters have been running longer than the port has been down,
    # and `show sys-info` is read with absent_ok - a release that does not
    # answer it leaves uptime_days None. (A box up for less than a day reads
    # 0, which is below any threshold, so it lands on LIKELY the same way.)
    # Treating that as trustworthy promoted ports from LIKELY UNUSED to
    # UNUSED on exactly the evidence this module says not to trust, and UNUSED
    # is what makes the planner leave a cable unpatched.
    counters_trustworthy = (port.has_traffic is False
                            and uptime_days is not None
                            and uptime_days >= unused_after_days)
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
