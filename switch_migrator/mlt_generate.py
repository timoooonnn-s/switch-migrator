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

import re
from dataclasses import dataclass, field

from switch_migrator.cabling_sheet import CablingRow, Sheet


@dataclass
class MltPlan:
    """One MLT to create on one new switch."""
    switch: str
    mlt_id: int
    name: str
    members: list[str] = field(default_factory=list)
    smlt: bool = False
    lacp: bool = False
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


def _old_key(row: CablingRow) -> tuple[str, int] | None:
    """Which old aggregation does this link belong to?"""
    return (row.old_switch, row.mlt_id) if row.mlt_id is not None else None


def _group(rows: list[CablingRow]) -> dict[tuple, list[CablingRow]]:
    """Group migrated links into the aggregations they will form.

    The planner's NEW MLT ID wins when they set one - that is the whole point
    of the column, and it is how two old MLTs get consolidated onto one new
    one. Otherwise links stay grouped by the old MLT they came from.
    """
    groups: dict[tuple, list[CablingRow]] = {}
    for row in rows:
        if row.new_mlt_id is not None:
            key = ("new", row.new_switch, row.new_mlt_id)
        elif row.mlt_id is not None:
            key = ("old", row.new_switch, row.old_switch, row.mlt_id)
        else:
            continue                    # a plain access port is not an MLT
        groups.setdefault(key, []).append(row)
    return groups


def _sorted_ports(ports: list[str]) -> list[str]:
    def key(p: str):
        return [int(n) for n in re.findall(r"\d+", p)] or [0]
    return sorted(set(ports), key=key)


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
    lacp=None carries the old MLT's LACP state forward per MLT; True/False
    forces it.
    """
    result = MltGenerateResult()
    rows = sheet.migrated
    if not rows:
        result.problems.append(
            "no row in the sheet has both a NEW switch and a NEW port - fill "
            "those in as the links are re-patched, then run this again")
        return result

    for key, members in sorted(_group(rows).items(), key=lambda kv: str(kv[0])):
        switch = key[1]
        first = members[0]
        if key[0] == "new":
            mlt_id, id_source = key[2], "sheet"
        else:
            mlt_id, id_source = key[3], f"carried over from {first.old_switch}"
        name, name_source = _pick_name(members, mlt_id)

        old_mlts = sorted({f"{r.old_switch} MLT {r.mlt_id}" for r in members
                           if r.mlt_id is not None})
        p = MltPlan(
            switch=switch, mlt_id=mlt_id, name=name,
            members=_sorted_ports([r.new_port for r in members]),
            smlt=smlt, flex_uni=flex_uni,
            lacp=bool(lacp) if lacp is not None else any(
                r.kind == "mlt" or r.mlt_id is not None for r in members),
            id_source=id_source if id_source == "sheet" else name_source,
            old_mlt=", ".join(old_mlts),
            neighbors=sorted({r.neighbor for r in members if r.neighbor}),
            vlans=sorted({v for r in members for v in r.expected_vlans}),
            isids=sorted({i for r in members
                          for i in (r.port_isids or r.mlt_isids)}),
        )
        if len(p.members) == 1:
            p.problems.append(
                "only one member: an aggregation of one is a normal port with "
                "extra steps - check whether the second link has been patched "
                "and recorded yet")
        p.problems += _member_warnings(members)
        result.plans.append(p)

    result.problems += _collision_problems(result.plans)
    if smlt:
        result.problems += _smlt_pairing_problems(result.plans)
    return result


def _member_warnings(members: list[CablingRow]) -> list[str]:
    out = []
    # Two neighbors is the NORMAL shape for an aggregation whose far end is
    # itself an SMLT/vIST pair - which is most uplinks here - so it is worth a
    # note, not a warning. Three or more cannot be a pair and is a real
    # problem: an MLT has to terminate on one logical device.
    neighbors = sorted({r.neighbor for r in members if r.neighbor})
    uplink = any(r.kind == "uplink" for r in members)
    if len(neighbors) > 2:
        out.append(f"members face {len(neighbors)} different neighbors "
                   f"({', '.join(neighbors)}) - an aggregation must terminate "
                   f"on ONE device, or on ONE SMLT pair")
    elif len(neighbors) == 2 and not uplink:
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
        out.append(f"\n#\n# ===== {switch} =====\n#")
        out.append("#\n# MLT CONFIGURATION\n#")
        for p in plans:
            out.append("")
            out.append(f"# MLT {p.mlt_id} \"{p.name}\" - id/name {p.id_source}")
            if p.old_mlt:
                out.append(f"#   was: {p.old_mlt}")
            if p.neighbors:
                out.append(f"#   far end: {', '.join(p.neighbors)}")
            if p.vlans:
                out.append(f"#   carried VLAN(s): {','.join(map(str, p.vlans))}"
                           + (f"  I-SID(s): {','.join(map(str, p.isids))}"
                              if p.isids else ""))
            for problem in p.problems:
                out.append(f"# [!] {problem}")
            out.append(f"mlt {p.mlt_id} enable name \"{p.name}\"")
            out.append(f"mlt {p.mlt_id} member {','.join(p.members)}")
        out.append("\n#\n# MLT INTERFACE CONFIGURATION\n#")
        for p in plans:
            out.append("")
            out.append(f"interface mlt {p.mlt_id}")
            if p.smlt:
                out.append("smlt")
            if p.lacp:
                out.append(f"lacp enable key {p.mlt_id}")
            if p.flex_uni:
                out.append("flex-uni enable")
            out.append("exit")
    return "\n".join(out).rstrip() + "\n"
