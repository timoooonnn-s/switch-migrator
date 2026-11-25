"""Generate the VOSS MLT configuration blocks for the new switches.

Input is the filled-in cabling sheet: the technicians record which NEW switch
and NEW port each old link lands on, and the planner may fill in a NEW MLT ID /
NEW MLT name. From that this works out which new ports belong together in one
aggregation and emits the config to create it.

Two things it deliberately does NOT do:

* it never invents an I-SID or a VLAN binding - the service configuration comes
  from --extract-config and the I-SID decision worksheet, which have their own
  rules about what may be guessed;
* it never emits a block for an MLT it only half understands. An MLT whose
  members ended up on two different new switches, or whose id collides with a
  different MLT, is reported as a problem instead.

Emitted syntax follows the sections a VOSS/Fabric Engine box prints in its own
`show running-config` (MLT CONFIGURATION, then MLT INTERFACE CONFIGURATION):

    mlt 35 enable name "MLT035"
    mlt 35 member 1/1,1/2

    interface mlt 35
    smlt
    lacp enable key 35
    flex-uni enable
    exit

SMLT: the access MLTs on these leaves are SMLT pairs, so the same MLT id has to
exist on BOTH vIST peers with the same members-facing-the-same-device. The
generator emits a block per new switch and, when an SMLT MLT only appears on
one of them, says so - an SMLT configured on one peer only is a single point of
failure wearing the costume of a redundant one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from switch_migrator import usage
from switch_migrator.cabling_sheet import CablingRow, Sheet
from switch_migrator.parsers.common import port_sort_key

# the far end of an SMLT is a vIST pair: two neighbours
SMLT_PAIR = 2


@dataclass
class MltPlan:
    """One MLT to create on one new switch."""
    switch: str
    mlt_id: int
    name: str
    members: list[str] = field(default_factory=list)
    smlt: bool = False
    lacp: bool | None = False   # None: the sheet did not say
    flex_uni: bool = False
    # where the id/name came from, so the file can say so
    id_source: str = ""            # sheet / carried over from <old switch>
    old_mlt: str = ""              # "gx-01 MLT 35" for the comment
    neighbors: list[str] = field(default_factory=list)
    vlans: list[int] = field(default_factory=list)
    isids: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class MltGenerateResult:
    plans: list[MltPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def by_switch(self) -> dict[str, list[MltPlan]]:
        out: dict[str, list[MltPlan]] = {}
        for plan in self.plans:
            out.setdefault(plan.switch, []).append(plan)
        return out


class _GroupKey(NamedTuple):
    """One aggregation-to-be on one new switch."""
    switch: str          # the NEW switch
    mlt_id: int
    old_switch: str      # '' when the planner's NEW MLT ID is what groups it

    @property
    def from_sheet(self) -> bool:
        return not self.old_switch

    @property
    def order(self) -> str:
        """Blocks come out sheet-numbered first, then by switch and id - kept
        as the text order the file has always had."""
        if self.from_sheet:
            return str(("new", self.switch, self.mlt_id))
        return str(("old", self.switch, self.old_switch, self.mlt_id))


def _group(rows: list[CablingRow]) -> dict[_GroupKey, list[CablingRow]]:
    """Group migrated links into the aggregations they will form.

    The planner's NEW MLT ID wins when they set one - that is the whole point
    of the column, and it is how two old MLTs get consolidated onto one new
    one. Otherwise links stay grouped by the old MLT they came from.
    """
    groups: dict[_GroupKey, list[CablingRow]] = {}
    for row in rows:
        if row.new_mlt_id is not None:
            key = _GroupKey(row.new_switch, row.new_mlt_id, "")
        elif row.mlt_id is not None:
            key = _GroupKey(row.new_switch, row.mlt_id, row.old_switch)
        else:
            continue                    # a plain access port is not an MLT
        groups.setdefault(key, []).append(row)
    return groups


def _sorted_ports(ports: list[str]) -> list[str]:
    return sorted(set(ports), key=port_sort_key)


def _pick_name(rows: list[CablingRow], mlt_id: int) -> tuple[str, str]:
    """(name, where it came from). Sheet first, old name as the fallback."""
    for row in rows:
        if row.new_mlt_name:
            return row.new_mlt_name, "sheet"
    for row in rows:
        if row.mlt_name:
            return row.mlt_name, f"carried over from {row.old_switch}"
    return f"MLT{mlt_id:03d}", "generated"


def plan(sheet: Sheet, smlt: bool = True, lacp: bool | None = None,
         flex_uni: bool = True) -> MltGenerateResult:
    """Work out the MLTs to create from a filled-in cabling sheet.

    smlt/flex_uni describe the target design (SMLT pairs on flex-UNI leaves).
    lacp=None carries the old MLT's LACP state forward per MLT, from the
    sheet's LACP column; where the sheet does not say, no LACP line is emitted
    and the MLT says so. True/False forces it for every MLT.
    """
    result = MltGenerateResult()
    rows = sheet.migrated
    if not rows:
        result.problems.append(
            "no row in the sheet has both a NEW switch and a NEW port - fill "
            "those in as the links are re-patched, then run this again")
        return result

    for key, members in sorted(_group(rows).items(), key=lambda kv: kv[0].order):
        switch, mlt_id = key.switch, key.mlt_id
        first = members[0]
        id_source = "sheet" if key.from_sheet else f"carried over from {first.old_switch}"
        name, name_source = _pick_name(members, mlt_id)

        old_mlts = sorted({f"{r.old_switch} MLT {r.mlt_id}" for r in members
                           if r.mlt_id is not None})
        p = MltPlan(
            switch=switch, mlt_id=mlt_id, name=name,
            members=_sorted_ports([r.new_port for r in members]),
            smlt=smlt, flex_uni=flex_uni,
            lacp=lacp if lacp is not None else _lacp_from_sheet(members),
            id_source=id_source if id_source == "sheet" else name_source,
            old_mlt=", ".join(old_mlts),
            neighbors=sorted({r.neighbor for r in members if r.neighbor}),
            vlans=sorted({v for r in members for v in r.expected_vlans}),
            isids=sorted({i for r in members
                          for i in (r.port_isids or r.mlt_isids)}),
        )
        if p.lacp is None:
            p.problems.append(
                "LACP unknown: the sheet does not say whether the old "
                "aggregation ran LACP, so no 'lacp' line is emitted - add "
                "'lacp enable key <id>' by hand if the far end runs it")
        elif lacp is None and len({r.lacp for r in members} - {None}) > 1:
            p.problems.append(
                "the old member links disagree on LACP (some yes, some no) - "
                "LACP is emitted; check the far end")
        not_yet = _members_not_recorded(sheet.rows, members)
        if not_yet:
            p.problems.append(
                f"{not_yet} more link(s) of the old aggregation have no NEW "
                f"switch/port yet - this block is incomplete until they do")
        elif len(p.members) == 1 and not smlt:
            # a one-member SMLT leg per peer is the normal dual-homed server
            # shape; a one-member plain MLT is a port with extra steps
            p.problems.append(
                "only one member: an aggregation of one on a single switch is "
                "a normal port with extra steps")
        p.problems += _member_warnings(members)
        result.plans.append(p)

    result.problems += _collision_problems(result.plans)
    if smlt:
        result.problems += _smlt_pairing_problems(result.plans)
    return result


def _lacp_from_sheet(members: list[CablingRow]) -> bool | None:
    """The old aggregation's LACP state, as the sheet carries it. Yes wins over
    no (a member that ran LACP proves the far end speaks it); None when no row
    says either - LACP is never invented."""
    states = {r.lacp for r in members} - {None}
    if True in states:
        return True
    if states == {False}:
        return False
    return None


def _members_not_recorded(all_rows: list[CablingRow],
                          members: list[CablingRow]) -> int:
    """Links of the same old aggregation(s) that have no NEW switch/port yet.

    Counted over the whole sheet, not per new switch: the two legs of a
    dual-homed server land on two different new switches by design, so one
    member per new switch is complete as long as nothing is still pending.
    """
    old = {(r.old_switch, r.mlt_id) for r in members if r.mlt_id is not None}
    # a member the usage classification called dead is not expected to move
    dead = {usage.UNUSED.lower(), usage.LIKELY_UNUSED.lower()}
    return sum(1 for r in all_rows
               if not r.migrated and (r.old_switch, r.mlt_id) in old
               and r.usage.strip().lower() not in dead)


def _member_warnings(members: list[CablingRow]) -> list[str]:
    out = []
    # Two neighbors is the NORMAL shape for an aggregation whose far end is
    # itself an SMLT/vIST pair - which is most uplinks here - so it is worth a
    # note, not a warning. Three or more cannot be a pair and is a real
    # problem: an MLT has to terminate on one logical device.
    neighbors = sorted({r.neighbor for r in members if r.neighbor})
    uplink = any(r.kind == "uplink" for r in members)
    if len(neighbors) > SMLT_PAIR:
        out.append(f"members face {len(neighbors)} different neighbors "
                   f"({', '.join(neighbors)}) - an aggregation must terminate "
                   f"on ONE device, or on ONE SMLT pair")
    elif len(neighbors) == SMLT_PAIR and not uplink:
        out.append(f"members face two neighbors ({', '.join(neighbors)}) - "
                   f"correct if they are a vIST/SMLT pair, wrong if they are "
                   f"two independent devices")
    old = {(r.old_switch, r.mlt_id) for r in members if r.mlt_id is not None}
    if len(old) > 1:
        out.append(f"consolidates {len(old)} old MLTs into one - intended?")
    for row in members:
        for problem in row.problems:
            out.append(f"sheet row {row.row_number}: {problem}")
    return out


def _collision_problems(plans: list[MltPlan]) -> list[str]:
    """Two different aggregations landing on the same id on one switch."""
    out = []
    seen: dict[tuple[str, int], MltPlan] = {}
    for p in plans:
        key = (p.switch, p.mlt_id)
        if key in seen:
            other = seen[key]
            out.append(
                f"{p.switch}: MLT {p.mlt_id} is claimed twice - by "
                f"{other.old_mlt or other.name} and by {p.old_mlt or p.name}. "
                f"Set a NEW MLT ID on one of them in the sheet.")
        else:
            seen[key] = p
    return out


def _smlt_pairing_problems(plans: list[MltPlan]) -> list[str]:
    """An SMLT wants the same id on both vIST peers.

    We cannot know from the sheet alone which two new switches are peers, so
    this reports the shape rather than asserting it: an MLT id that exists on
    exactly one new switch, while other ids exist on two, is the suspicious
    case worth a human look.
    """
    switches_by_id: dict[int, set[str]] = {}
    for p in plans:
        switches_by_id.setdefault(p.mlt_id, set()).add(p.switch)
    paired = {i for i, s in switches_by_id.items() if len(s) > 1}
    if not paired:
        return []                       # single-leaf migration: nothing to pair
    lonely = sorted(i for i, s in switches_by_id.items() if len(s) == 1)
    if not lonely:
        return []
    return [f"MLT {i} exists on only one new switch ({next(iter(switches_by_id[i]))}) "
            f"while other MLTs in this sheet are configured on both peers - an "
            f"SMLT on one peer only is a single point of failure that looks "
            f"redundant" for i in lonely]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_HEADER = """\
# ==========================================================================
# GENERATED MLT CONFIGURATION - REVIEW BEFORE PASTING
# Source: {source}
# Generated by switch-migrator from the filled-in cabling sheet.
#
# What this is: the link aggregations to create on the NEW switches, derived
# from which new ports the technicians recorded for each old MLT member.
#
# What this is NOT: the service configuration. No I-SID, c-vid or VLAN binding
# is emitted here - those come from --extract-config and the I-SID decision
# worksheet, where a guess is never silently made.
#
# YOU MUST VERIFY:
#   - that each MLT's members really terminate on ONE device at the far end;
#   - the SMLT peer: the same MLT id must exist on BOTH vIST peers, with the
#     members that face the same device;
#   - LACP: 'lacp enable key <id>' only belongs here if the far end runs it.
#     A key mismatch brings the aggregation up in a way that looks fine and
#     forwards nothing.
# ==========================================================================
"""


def render_sections(plans: list[MltPlan],
                    interface_extra: dict[int, list[str]] | None = None
                    ) -> tuple[list[str], list[str]]:
    """The two sections a VOSS box prints for its MLTs, for one switch:
    (MLT CONFIGURATION lines, MLT INTERFACE CONFIGURATION lines). Shared by
    --generate-mlt and the per-new-switch config; `interface_extra` adds lines
    to an MLT's interface block (by MLT id)."""
    definitions: list[str] = []
    interfaces: list[str] = []
    for p in plans:
        definitions.append("")
        definitions.append(f"# MLT {p.mlt_id} \"{p.name}\" - id/name {p.id_source}")
        if p.old_mlt:
            definitions.append(f"#   was: {p.old_mlt}")
        if p.neighbors:
            definitions.append(f"#   far end: {', '.join(p.neighbors)}")
        if p.vlans:
            definitions.append(f"#   carried VLAN(s): {','.join(map(str, p.vlans))}"
                               + (f"  I-SID(s): {','.join(map(str, p.isids))}"
                                  if p.isids else ""))
        for problem in p.problems:
            definitions.append(f"# [!] {problem}")
        definitions.append(f"mlt {p.mlt_id} enable name \"{p.name}\"")
        definitions.append(f"mlt {p.mlt_id} member {','.join(p.members)}")
    for p in plans:
        interfaces.append("")
        interfaces.append(f"interface mlt {p.mlt_id}")
        if p.smlt:
            interfaces.append("smlt")
        if p.lacp:
            interfaces.append(f"lacp enable key {p.mlt_id}")
        if p.flex_uni:
            interfaces.append("flex-uni enable")
        interfaces += (interface_extra or {}).get(p.mlt_id, [])
        interfaces.append("exit")
    return definitions, interfaces


def render(result: MltGenerateResult, source: str = "") -> str:
    out = [_HEADER.format(source=source or "cabling sheet").rstrip()]
    if result.problems:
        out.append("\n#\n# PROBLEMS FOUND - read these first\n#")
        for problem in result.problems:
            out.append(f"# [!] {problem}")
    if not result.plans:
        out.append("\n# (no MLT could be derived from the sheet)")
        return "\n".join(out) + "\n"

    for switch, plans in sorted(result.by_switch.items()):
        definitions, interfaces = render_sections(plans)
        out.append(f"\n#\n# ===== {switch} =====\n#")
        out.append("#\n# MLT CONFIGURATION\n#")
        out += definitions
        out.append("\n#\n# MLT INTERFACE CONFIGURATION\n#")
        out += interfaces
    return "\n".join(out).rstrip() + "\n"
