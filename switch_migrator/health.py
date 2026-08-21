"""Pre-migration health check: is this switch in a fit state to be migrated?

The audit answers "does the config match the fabric". This answers a different
and more urgent question, asked at the start of the maintenance window: is
anything already broken that the migration would make worse, or that would hide
behind the migration and get blamed on it afterwards?

A degraded MLT is the clearest example. Migrating a link aggregation that is
already running on one leg means the first cable you unplug is an outage, not a
re-patch. Likewise a vIST session that is down: the SMLT pair is not a pair
right now, and moving either half of it is a different, riskier operation than
the one that was planned.

Every check is derived from the state the normal collection already gathers -
no extra command is sent to any device, so running the health check costs
nothing beyond the audit you were doing anyway.

Verdicts, worst first:
  BLOCK  - do not start; fix this or re-plan the window
  WARN   - start, but know about it and watch it
  OK     - nothing found
  UNKNOWN- the switch did not tell us enough to judge
"""

from __future__ import annotations

from dataclasses import dataclass, field

from switch_migrator import usage
from switch_migrator.config import Config
from switch_migrator.models import CompStatus, SwitchAudit, VlanComparison

BLOCK = "BLOCK"
WARN = "WARN"
OK = "OK"
UNKNOWN = "UNKNOWN"

# worst first, for sorting and for the overall verdict
_RANK = {BLOCK: 0, WARN: 1, UNKNOWN: 2, OK: 3}

# session-setup problems that make the collected data less trustworthy
_SESSION_WARNING_MARKERS = ("paging", "privileged", "may stall",
                            "terminal more", "terminal length")


@dataclass
class Finding:
    """One thing worth knowing before the window opens."""
    switch: str
    verdict: str
    check: str
    detail: str
    action: str = ""

    @property
    def severity(self) -> str:
        return {BLOCK: "error", WARN: "warn"}.get(self.verdict, "ok")


@dataclass
class HealthReport:
    findings: list[Finding] = field(default_factory=list)
    by_switch: dict[str, str] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """The whole run's verdict: the worst any switch got."""
        if not self.by_switch:
            return UNKNOWN
        return min(self.by_switch.values(), key=lambda v: _RANK[v])

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == BLOCK]

    def counts(self) -> dict[str, int]:
        return {v: sum(1 for s in self.by_switch.values() if s == v)
                for v in (BLOCK, WARN, UNKNOWN, OK)}


def _worst(verdicts: list[str]) -> str:
    return min(verdicts, key=lambda v: _RANK[v]) if verdicts else OK


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def _check_reachable(a: SwitchAudit) -> list[Finding]:
    if a.reachable:
        return []
    reason = a.errors[-1] if a.errors else "unknown error"
    return [Finding(a.name, BLOCK, "reachable",
                    f"the switch could not be read: {reason}",
                    "fix access before the window - a device you cannot read "
                    "is a device you cannot verify afterwards")]


def _check_data_completeness(a: SwitchAudit) -> list[Finding]:
    """Did we actually learn enough about this box to migrate it safely?"""
    out = []
    if not a.ports:
        out.append(Finding(
            a.name, BLOCK, "port state",
            "no port state could be read from this switch",
            "without it there is no cabling sheet and nothing to verify "
            "against afterwards - check the account's privilege level"))
    if not a.vlans:
        out.append(Finding(
            a.name, WARN, "vlan state", "no VLANs were read from this switch",
            "the VLAN/I-SID columns of every sheet will be empty"))
    for warning in a.warnings:
        # Session-setup warnings mean the data we DO have may be truncated.
        # Matched on the consequence rather than one spelling: a rejected
        # paging command is reported as "long command outputs may stall",
        # which never contains the word 'paging'.
        if any(m in warning.lower() for m in _SESSION_WARNING_MARKERS):
            out.append(Finding(a.name, WARN, "session", warning,
                               "output from this device may be incomplete"))
    return out


def _check_ist(a: SwitchAudit) -> list[Finding]:
    """A vIST that is down means the SMLT pair is not a pair right now."""
    if a.ist is None or a.ist.enabled is None:
        return []
    if not a.ist.enabled:
        return []                       # no IST configured: nothing to be down
    if a.ist.session_up is True:
        return []
    if a.ist.session_up is None:
        return [Finding(a.name, UNKNOWN, "vIST",
                        "vIST is enabled but the session state could not be read",
                        "confirm the peer link by hand before starting")]
    peer = f" (peer {a.ist.peer_ip})" if a.ist.peer_ip else ""
    return [Finding(a.name, BLOCK, "vIST",
                    f"vIST is enabled but the session is DOWN{peer}",
                    "the SMLT pair is split right now - migrating either half "
                    "is a different, riskier operation than the one planned")]


def _check_mlts(a: SwitchAudit) -> list[Finding]:
    out = []
    for m in a.mlts:
        if not m.members:
            continue                    # an empty MLT id is config residue
        if m.members_up is None:
            if m.in_datapath is False:
                out.append(Finding(
                    a.name, WARN, f"MLT {m.mlt_id}",
                    f"'{m.name}' is not programmed in the data path",
                    "it is carrying nothing today - confirm it is meant to "
                    "exist on the new switch at all"))
            continue
        if m.members_up == 0:
            out.append(Finding(
                a.name, WARN, f"MLT {m.mlt_id}",
                f"'{m.name}' has no member up ({m.members_up_display})",
                "already dead before the migration - do not let it be "
                "reported afterwards as something the migration broke"))
        elif m.members_up < m.members_total:
            down = m.members_total - m.members_up
            out.append(Finding(
                a.name, BLOCK, f"MLT {m.mlt_id}",
                f"'{m.name}' is running degraded: {m.members_up_display} "
                f"member(s) up, {down} down",
                "there is no redundancy left on this LAG - the first cable "
                "you unplug is an outage, not a re-patch"))
    return out


def _check_uplinks(a: SwitchAudit) -> list[Finding]:
    total, up = a.uplink_ports_total, a.uplink_ports_up
    if not total:
        return [Finding(a.name, WARN, "uplinks",
                        "no uplink port could be identified by LLDP",
                        "check core_switch_patterns in the config - without "
                        "it the sheets cannot tell an uplink from an access "
                        "port")]
    if up == 0:
        return [Finding(a.name, BLOCK, "uplinks",
                        f"none of the {total} uplink port(s) are up",
                        "this switch is isolated right now")]
    if up < total:
        return [Finding(a.name, BLOCK, "uplinks",
                        f"only {up} of {total} uplink port(s) are up",
                        "the switch is running without uplink redundancy")]
    return []


def _check_degraded_ports(a: SwitchAudit) -> list[Finding]:
    """Ports down on a still-forwarding MLT: a fault on a live cable.

    The usage classification marks these DEGRADED, but it only runs when MACs
    were pulled (--migration-sheets). When it has not run - p.usage is empty on
    every port - the same rule is applied directly to the port/MLT state here,
    so a plain --health-check is never a silent no-op.
    """
    degraded = [p for p in a.ports if p.usage == usage.DEGRADED]
    if not any(p.usage for p in a.ports):
        by_mlt = {m.mlt_id: m for m in a.mlts}
        degraded = []
        for p in a.ports:
            if p.oper_up is not False or p.macs or p.mlt_id is None:
                continue
            mlt = by_mlt.get(p.mlt_id)
            if mlt is not None and (mlt.in_datapath is True
                                    or bool(mlt.members_up)):
                degraded.append(p)
    if not degraded:
        return []
    listed = ", ".join(p.port for p in degraded[:8])
    more = f" (+{len(degraded) - 8} more)" if len(degraded) > 8 else ""
    return [Finding(a.name, WARN, "degraded ports",
                    f"{len(degraded)} port(s) down on a still-forwarding MLT: "
                    f"{listed}{more}",
                    "a cable or optic is already faulty - fix or document it "
                    "now, so it is not discovered during the cutover")]


def _check_comparisons(a: SwitchAudit,
                       comps: list[VlanComparison]) -> list[Finding]:
    """Fabric problems that would strand a VLAN after the move."""
    errors = [c for c in comps if c.severity == "error"]
    if not errors:
        return []
    by_status: dict[CompStatus, list[int]] = {}
    for c in errors:
        by_status.setdefault(c.status, []).append(c.vlan_id)
    out = []
    for status, vlans in by_status.items():
        listed = ", ".join(str(v) for v in sorted(vlans)[:10])
        more = f" (+{len(vlans) - 10} more)" if len(vlans) > 10 else ""
        out.append(Finding(
            a.name, BLOCK, "VLAN vs fabric",
            f"{len(vlans)} VLAN(s) {status.value}: {listed}{more}",
            "decide the I-SID for these before the window - after the move "
            "they have nowhere to land"))
    return out


_CHECKS = (_check_data_completeness, _check_ist, _check_mlts, _check_uplinks,
           _check_degraded_ports)


def check(audits: list[SwitchAudit],
          comparisons: dict[str, list[VlanComparison]] | None = None,
          cfg: Config | None = None) -> HealthReport:
    """Run every check against already-collected state. Sends no commands."""
    comparisons = comparisons or {}
    report = HealthReport()
    for a in sorted(audits, key=lambda x: x.name):
        findings: list[Finding] = _check_reachable(a)
        if a.reachable:
            for fn in _CHECKS:
                findings += fn(a)
            findings += _check_comparisons(a, comparisons.get(a.name, []))
        report.findings += findings
        report.by_switch[a.name] = _worst([f.verdict for f in findings])
    report.findings.sort(key=lambda f: (_RANK[f.verdict], f.switch, f.check))
    return report
