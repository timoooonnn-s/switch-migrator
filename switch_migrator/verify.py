"""Post-migration verification: did every link come back up where it should?

Input is the filled-in cabling sheet plus a fresh collection of the NEW
switches. For every row a technician has recorded a NEW switch and port for,
this checks that the link is actually there and carrying what it used to.

The pass rule, in order of how much it proves:

  PASS  the new port is up AND at least one MAC the old port used to learn is
        now learned on it. That is the strongest evidence available that the
        right cable went into the right hole: the same machine is talking on
        the new port.
  WARN  the port is up but nothing else lines up yet - no expected MAC has
        reappeared, or the LLDP neighbor or VLANs differ from the sheet. A
        quiet host that has not spoken since the move looks exactly like this,
        and so does a genuine mistake, which is why it is neither a pass nor a
        failure but a thing for a human to glance at.
  FAIL  the new port is down, missing from the switch, or the switch itself
        could not be read. Something is wrong with this link right now.
  PENDING  the row has no NEW switch/port yet - not migrated, not a problem.

MACs age out (typically five minutes) and a machine that has not sent a frame
since the cutover has no entry anywhere, so 'no MAC yet' is reported as a warn
with that explanation rather than as a failure. Run the verification again a
few minutes later and most warns turn into passes on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from switch_migrator.cabling_sheet import CablingRow, Sheet
from switch_migrator.models import PortState, SwitchAudit

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
PENDING = "PENDING"

_RANK = {FAIL: 0, WARN: 1, PENDING: 2, PASS: 3}


@dataclass
class PortVerdict:
    uid: str
    old_switch: str
    old_port: str
    new_switch: str
    new_port: str
    result: str
    reasons: list[str] = field(default_factory=list)
    # what was actually found, for the report's evidence columns
    link: str = ""                       # up / down / not found
    macs_expected: int = 0
    macs_found: int = 0
    neighbor_expected: str = ""
    neighbor_found: str = ""
    vlans_expected: list[int] = field(default_factory=list)
    vlans_found: list[int] = field(default_factory=list)
    mlt_expected: int | None = None
    mlt_found: int | None = None

    @property
    def severity(self) -> str | None:
        # None for PENDING - the neutral 'no style' key the console renderer
        # uses; the annotation says so instead of promising a str
        return {FAIL: "error", WARN: "warn", PASS: "ok"}.get(self.result)

    @property
    def why(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class VerifyReport:
    verdicts: list[PortVerdict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {r: sum(1 for v in self.verdicts if v.result == r)
                for r in (PASS, WARN, FAIL, PENDING)}

    @property
    def ok(self) -> bool:
        return not any(v.result == FAIL for v in self.verdicts)


def _index_ports(audit: SwitchAudit) -> dict[str, PortState]:
    return {p.port: p for p in audit.ports}


def _norm_macs(macs: list[str]) -> set[str]:
    return {m.lower().replace("-", ":") for m in macs if m}


def _same_neighbor(expected: str, found: str) -> bool:
    """Neighbor identity is fuzzy on purpose.

    The sheet records whatever identified the device best - a sysname, or an IP
    or a SysDescr when it had no name. After the move the new switch may report
    a different one of those three for the same machine, so a match on either
    direction of containment counts.
    """
    e, f = expected.strip().lower(), found.strip().lower()
    if not e or not f:
        return False
    return e == f or e in f or f in e


def _verify_row(row: CablingRow, audit: SwitchAudit | None,
                ports: dict[str, PortState]) -> PortVerdict:
    v = PortVerdict(uid=row.uid, old_switch=row.old_switch,
                    old_port=row.old_port, new_switch=row.new_switch,
                    new_port=row.new_port, result=FAIL,
                    neighbor_expected=row.neighbor,
                    vlans_expected=row.expected_vlans,
                    macs_expected=len(row.macs),
                    mlt_expected=row.new_mlt_id if row.new_mlt_id is not None
                    else row.mlt_id)

    if audit is None:
        v.reasons.append(f"'{row.new_switch}' was not collected - add it to "
                         f"the inventory or -s to verify this link")
        v.link = "not collected"
        return v
    if not audit.reachable:
        v.reasons.append(f"{row.new_switch} could not be read")
        v.link = "unreachable"
        return v

    port = ports.get(row.new_port)
    if port is None:
        v.reasons.append(f"port {row.new_port} does not exist on "
                         f"{row.new_switch} - check the sheet entry")
        v.link = "not found"
        return v

    v.link = "up" if port.oper_up else ("down" if port.oper_up is False else "?")
    v.neighbor_found = port.lldp_neighbor or port.lldp_neighbor_ip or ""
    v.vlans_found = list(port.vlans)
    v.mlt_found = port.mlt_id
    found_macs = _norm_macs(port.macs)
    overlap = _norm_macs(row.macs) & found_macs
    v.macs_found = len(overlap)

    if port.oper_up is False:
        reason = f" (reason {port.state_reason})" if port.state_reason else ""
        v.reasons.append(f"link is DOWN on {row.new_switch} {row.new_port}{reason}")
        return v
    if port.oper_up is None:
        v.result = WARN
        v.reasons.append("port state could not be read on the new switch")
        return v

    # link is up: how much of the old identity came with it?
    notes: list[str] = []
    if row.macs and overlap:
        v.result = PASS
        notes.append(f"link up, {len(overlap)} of {len(row.macs)} expected "
                     f"MAC(s) learned here")
    elif row.macs:
        v.result = WARN
        notes.append(f"link up, but none of the {len(row.macs)} expected MAC(s) "
                     f"have appeared yet - a host that has not sent a frame "
                     f"since the cutover looks like this; re-run in a few "
                     f"minutes")
    else:
        # nothing was learned on the old port either - the sheet has no MAC to
        # match, so link state plus whatever else agrees is all there is
        v.result = WARN
        notes.append("link up, but the old port had no learned MAC to match "
                     "against")

    if row.neighbor:
        if _same_neighbor(row.neighbor, v.neighbor_found):
            notes.append(f"LLDP neighbor matches ({v.neighbor_found})")
            if v.result == WARN and not row.macs:
                # neighbor identity is nearly as good as a MAC: the far end
                # announced itself on this port
                v.result = PASS
        elif v.neighbor_found:
            notes.append(f"LLDP neighbor is '{v.neighbor_found}', the sheet "
                         f"says '{row.neighbor}'")
            v.result = WARN
        else:
            notes.append(f"no LLDP neighbor seen (sheet says '{row.neighbor}')")

    expected_vlans = set(row.expected_vlans)
    if expected_vlans:
        missing = sorted(expected_vlans - set(v.vlans_found))
        if missing and v.vlans_found:
            notes.append(f"VLAN(s) {','.join(map(str, missing))} not on the "
                         f"new port (it has {','.join(map(str, v.vlans_found))})")
            v.result = WARN
        elif missing:
            notes.append(f"no VLAN binding read on the new port (expected "
                         f"{','.join(map(str, sorted(expected_vlans)))})")

    if v.mlt_expected is not None:
        if v.mlt_found is None:
            notes.append(f"expected to be a member of MLT {v.mlt_expected}, "
                         f"but the port is not in any MLT")
            v.result = WARN
        elif v.mlt_found != v.mlt_expected:
            notes.append(f"is in MLT {v.mlt_found}, the sheet says "
                         f"MLT {v.mlt_expected}")
            v.result = WARN

    v.reasons = notes
    return v


def verify(sheet: Sheet, audits: list[SwitchAudit]) -> VerifyReport:
    """Check every migrated row of the sheet against the new switches."""
    report = VerifyReport()
    by_name = {a.name: a for a in audits}
    ports_by_name = {a.name: _index_ports(a) for a in audits}

    named = {r.new_switch for r in sheet.migrated}
    missing = sorted(n for n in named if n not in by_name)
    if missing:
        report.problems.append(
            f"the sheet names {len(missing)} switch(es) that were not "
            f"collected: {', '.join(missing)}")

    for row in sheet.rows:
        if not row.migrated:
            report.verdicts.append(PortVerdict(
                uid=row.uid, old_switch=row.old_switch, old_port=row.old_port,
                new_switch="", new_port="", result=PENDING,
                reasons=["no NEW switch/port recorded in the sheet yet"]))
            continue
        report.verdicts.append(_verify_row(
            row, by_name.get(row.new_switch),
            ports_by_name.get(row.new_switch, {})))

    report.verdicts.sort(key=lambda v: (_RANK[v.result], v.new_switch, v.uid))
    report.problems += [f"sheet row {r.row_number}: {p}"
                        for r in sheet.rows for p in r.problems]
    return report


def unexpected_ports(sheet: Sheet, audits: list[SwitchAudit]) -> list[tuple[str, str]]:
    """Ports that are up on a new switch but appear in no sheet row.

    The sheet says what should have moved; this is the other direction - a link
    somebody patched without writing it down. Not necessarily wrong (the new
    switch has uplinks of its own), but worth a look before signing off.
    """
    claimed: dict[str, set[str]] = {}
    for row in sheet.migrated:
        claimed.setdefault(row.new_switch, set()).add(row.new_port)
    out = []
    for audit in audits:
        if audit.name not in claimed:
            continue
        for port in audit.ports:
            if port.oper_up and port.port not in claimed[audit.name]:
                out.append((audit.name, port.port))
    return out
