"""The config for each NEW switch, from the plan (council decision D3).

One file per new switch, holding ONLY what this tool is for (round 2, R9/R10):

1. MLTs - definition and interface block, 4xx id, SMLT towards the peer, LACP only
   where the old aggregation ran it;
2. ports - in the two phases the switch itself prints (phase I `encapsulation dot1q`
   for ports that carry tagged traffic, phase II flex-UNI and the access hygiene
   lines from `plan.port_lines`);
3. I-SID services - flex-UNI `i-sid <id> elan` with `c-vid <vlan> port|mlt ...`
   and `untagged-traffic port|mlt ...`, on the NEW ports.

Everything ZTP owns (domain, DNS, syslog, SSH, SNMP, mgmt) and the vIST / NNI
uplinks have no code path here: they cannot appear by accident.

What goes on a port comes from the OLD port's VLAN bindings - which VLANs, tagged or
untagged - read from the old running-config. The I-SID per VLAN is resolved in the
order of `isid.resolve`: excluded -> explicit decision -> the I-SID the old switch or
the fabric already uses -> a commented-out [REVIEW] block plus a decision worksheet.
Nothing is guessed.

Lines no real capture shows yet are marked [UNVERIFIED] and listed in the header, so
the first box is where they get checked, not the hundredth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from switch_migrator import standard
from switch_migrator.config import Config
from switch_migrator.isid import IsidDecision, resolve
from switch_migrator.mlt_generate import MltPlan, render_sections
from switch_migrator.models import (
    BOTH,
    TAGGED,
    UNTAGGED,
    MltState,
    PortState,
    SwitchAudit,
    VlanBinding,
)
from switch_migrator.parsers.common import port_sort_key
from switch_migrator.plan import MigrationPlan, Move

# what no capture has shown yet - named in the header of every file that uses it
UNVERIFIED_CVID_MLT = ("'c-vid <vlan> mlt <id>' / 'untagged-traffic mlt <id>': the "
                       "parser knows the form, no captured config shows it")
UNVERIFIED_MLT_ENCAP = ("'encapsulation dot1q' inside 'interface mlt': not in any "
                        "capture")
UNVERIFIED_LACP_MEMBERS = ("LACP member ports: a captured LACP MLT had no 'mlt N "
                           "member' line, so VOSS may add LACP members from the port "
                           "side ('lacp key/aggregation' on the port) instead")


@dataclass
class NewSwitchConfig:
    switch: str
    text: str
    decisions: list[IsidDecision] = field(default_factory=list)   # need a human
    problems: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


@dataclass
class _Target:
    kind: str          # "port" or "mlt"
    ident: str         # '1/24' or '401'

    @property
    def sort(self) -> tuple:
        return (0 if self.kind == "mlt" else 1, port_sort_key(self.ident))


@dataclass
class _Service:
    isid: int
    source: str
    cvids: dict[int, list[_Target]] = field(default_factory=dict)
    untagged: list[_Target] = field(default_factory=list)


class _Builder:
    def __init__(self, switch: str, plan: MigrationPlan, audits: list[SwitchAudit],
                 cfg: Config):
        self.switch = switch
        self.plan = plan
        self.cfg = cfg
        self.ports = {(a.name, p.port): p for a in audits for p in a.ports}
        self.mlts = {(a.name, m.mlt_id): m for a in audits for m in a.mlts}
        self.vlan_names = {v.vlan_id: v.name for a in audits for v in a.vlans}
        self.services: dict[int, _Service] = {}
        self.review: dict[int, tuple[IsidDecision, list[tuple[str, _Target]]]] = {}
        self.notes: list[str] = []          # comments for the I-SID section
        self.problems: list[str] = []
        self.unverified: set[str] = set()
        self.decisions: dict[int, IsidDecision] = {}
        self.untagged_isid: dict[str, int] = {}   # target -> the I-SID it is untagged in

    # -- bindings -> services ------------------------------------------------
    def bind(self, target: _Target, bindings: list[VlanBinding], where: str) -> bool:
        """Add a port's or MLT's bindings to the services. True if anything on it
        egresses tagged (so a port needs phase I)."""
        tagged = False
        for b in bindings:
            kinds = {TAGGED: ("t",), UNTAGGED: ("u",), BOTH: ("t", "u")}.get(
                b.tagging, ("t",))
            if not b.tagging and b.vlan is not None:
                self.problems.append(
                    f"{where}: the old switch did not say whether VLAN {b.vlan} is "
                    f"tagged or untagged - emitted as a tagged c-vid; check it (was "
                    f"the old running-config read?)")
            if b.vlan is None:
                if b.isid is None:
                    continue
                self._add(b.isid, "the old switch", target, None, ("u",), where)
                continue
            if b.vlan in standard.RESERVED_VLAN_IDS:
                self.notes.append(f"# VLAN {b.vlan} on {where}: reserved estate-wide "
                                  f"- not configured")
                continue
            decision = resolve(b.vlan, self.vlan_names.get(b.vlan, ""), self.cfg, b.isid)
            if decision.excluded:
                self.notes.append(f"# VLAN {b.vlan} on {where}: excluded in config.yaml "
                                  f"- not configured")
                continue
            if decision.needs_decision:
                self.decisions[b.vlan] = decision
                self.review.setdefault(b.vlan, (decision, []))[1].append(
                    ("".join(k for k in kinds), target))
                if target.kind == "mlt":
                    self.unverified.add(UNVERIFIED_CVID_MLT)
                tagged = tagged or "t" in kinds
                continue
            source = {"explicit": "your decision in config.yaml",
                      "fabric": "the old switch / the fabric"}.get(decision.source,
                                                                    decision.source)
            self._add(decision.isid, source, target, b.vlan, kinds, where)
            tagged = tagged or "t" in kinds
        return tagged

    def _add(self, isid: int, source: str, target: _Target, vlan: int | None,
             kinds: tuple[str, ...], where: str) -> None:
        service = self.services.setdefault(isid, _Service(isid, source))
        if target.kind == "mlt":
            self.unverified.add(UNVERIFIED_CVID_MLT)
        if "t" in kinds and vlan is not None:
            service.cvids.setdefault(vlan, []).append(target)
        if "u" in kinds:
            key = f"{target.kind} {target.ident}"
            other = self.untagged_isid.get(key)
            if other is not None and other != isid:
                self.problems.append(
                    f"{where}: untagged traffic would land in I-SID {other} AND {isid} "
                    f"- one port has one untagged service; fix the old port's "
                    f"tagging in the sheet")
                return
            self.untagged_isid[key] = isid
            service.untagged.append(target)


def _lacp(ports: list[PortState]) -> bool | None:
    states = {p.lacp for p in ports} - {None}
    if True in states:
        return True
    return False if states == {False} else None


def _comment_for(move: Move, port: PortState | None) -> list[str]:
    seen = (port.lldp_neighbor or port.lldp_neighbor_ip) if port else ""
    head = (f"# {move.new_port} <- {move.link.old_switch} {move.link.old_port}"
            + (f" ({seen})" if seen else "") + f" - {move.rule}")
    return [head] + [f"# [!] {flag}" for flag in [*move.link.flags, *move.flags]]


def render_switch(switch: str, plan: MigrationPlan, audits: list[SwitchAudit],
                  cfg: Config) -> NewSwitchConfig:
    b = _Builder(switch, plan, audits, cfg)
    moves = sorted((m for m in plan.moves if m.new_switch == switch),
                   key=lambda m: port_sort_key(m.new_port))
    peer = next((f.peer for f in plan.fill if f.name == switch), "")

    # -- MLTs -------------------------------------------------------------------
    lags: dict[int, list[Move]] = {}
    plain: list[Move] = []
    for move in moves:
        (lags.setdefault(move.new_mlt_id, []) if move.new_mlt_id is not None
         else plain).append(move)
    mlt_plans: list[MltPlan] = []
    mlt_extra: dict[int, list[str]] = {}
    member_lines: list[str] = []
    for mlt_id, members in sorted(lags.items()):
        old_ports = [b.ports.get(m.link.key) for m in members]
        old_mlts: list[MltState] = [b.mlts[(m.link.old_switch, m.link.old_mlt_id)]
                                    for m in members if m.link.old_mlt_id is not None
                                    and (m.link.old_switch, m.link.old_mlt_id) in b.mlts]
        lacp = _lacp([p for p in old_ports if p is not None])
        problems = []
        if lacp is None:
            problems.append("LACP unknown: the old aggregation's LACP state was not "
                            "read - add 'lacp enable key <id>' by hand if the far "
                            "end runs it")
        bindings = (old_mlts[0].bindings if old_mlts and old_mlts[0].bindings else
                    [x for p in old_ports if p is not None for x in p.bindings])
        target = _Target("mlt", str(mlt_id))
        tagged = b.bind(target, _dedupe(bindings), f"MLT {mlt_id}")
        if tagged:
            b.unverified.add(UNVERIFIED_MLT_ENCAP)
            mlt_extra[mlt_id] = ["# [UNVERIFIED] encapsulation on an MLT",
                                 "encapsulation dot1q"]
        if lacp:
            b.unverified.add(UNVERIFIED_LACP_MEMBERS)
        mlt_plans.append(MltPlan(
            switch=switch, mlt_id=mlt_id, name=members[0].new_mlt_name or f"MLT{mlt_id}",
            members=sorted({m.new_port for m in members}, key=port_sort_key),
            smlt=bool(peer), lacp=lacp, flex_uni=True,
            id_source="from the plan (4xx of the first port)",
            old_mlt=", ".join(sorted({f"{m.link.old_switch} MLT {m.link.old_mlt_id}"
                                      for m in members if m.link.old_mlt_id is not None})),
            neighbors=sorted({p.lldp_neighbor for p in old_ports if p and p.lldp_neighbor}),
            vlans=sorted({x.vlan for x in bindings if x.vlan is not None}),
            problems=problems))
        for move in members:
            port = b.ports.get(move.link.key)
            member_lines += [""] + _comment_for(move, port) + [
                f"interface GigabitEthernet {move.new_port}"]
            if port and port.name:
                member_lines.append(f'name "{port.name}"')
            if lacp:
                member_lines.append("# [UNVERIFIED] LACP member port: check whether "
                                    "'lacp key/aggregation' is needed here")
            member_lines += ["no shutdown", "exit"]

    # -- plain ports --------------------------------------------------------------
    phase1: list[str] = []
    phase2: list[str] = []
    for move in plain:
        port = b.ports.get(move.link.key)
        target = _Target("port", move.new_port)
        tagged = b.bind(target, _dedupe(port.bindings if port else []),
                        f"{move.new_port} (old {move.link.old_switch} {move.link.old_port})")
        if port is None or not port.bindings:
            b.problems.append(f"{move.new_port} <- {move.link.old_switch} "
                              f"{move.link.old_port}: no VLAN on the old port - it gets "
                              f"no service; check the old running-config was read")
        if tagged:
            phase1 += ["", f"interface GigabitEthernet {move.new_port}",
                       "encapsulation dot1q", "exit"]
        phase2 += [""] + _comment_for(move, port) + [
            f"interface GigabitEthernet {move.new_port}", "default-vlan-id 0"]
        if port and port.name:
            phase2.append(f'name "{port.name}"')
        phase2.append("flex-uni enable")
        if port is not None and port.admin_up is False:
            phase2.append("# [!] was admin-down on the old switch")
            phase2.append("shutdown")
        else:
            phase2.append("no shutdown")
        phase2 += list(cfg.plan.port_lines) + ["exit"]

    definitions, interfaces = render_sections(mlt_plans, mlt_extra)
    text = _assemble(switch, peer, plan, moves, b, definitions, interfaces,
                     member_lines, phase1, phase2)
    return NewSwitchConfig(switch, text, sorted(b.decisions.values(),
                                                 key=lambda d: d.vlan_id),
                           b.problems, sorted(b.unverified))


def _dedupe(bindings: list[VlanBinding]) -> list[VlanBinding]:
    seen, out = set(), []
    for x in bindings:
        if (x.vlan, x.isid, x.tagging) not in seen:
            seen.add((x.vlan, x.isid, x.tagging))
            out.append(x)
    return out


def _targets(targets: list[_Target]) -> list[str]:
    """'port 1/1,1/24' and 'mlt 401' - one line per kind."""
    ports = sorted({t.ident for t in targets if t.kind == "port"}, key=port_sort_key)
    mlts = sorted({t.ident for t in targets if t.kind == "mlt"}, key=port_sort_key)
    return [f"mlt {m}" for m in mlts] + ([f"port {','.join(ports)}"] if ports else [])


def _assemble(switch, peer, plan, moves, b, definitions, interfaces, member_lines,
              phase1, phase2) -> str:
    bar = "# " + "=" * 74
    olds = sorted({m.link.old_switch for m in moves})
    out = [bar, f"# NEW SWITCH CONFIG - {switch} - GENERATED, REVIEW BEFORE PASTING",
           f"# From the plan: {len(moves)} link(s) from {', '.join(olds) or '-'}"
           + (f"; SMLT peer {peer}" if peer else "; no peer (no SMLT)"),
           f"# Access ports 1/1-1/{plan.layout.access_ports}; the last "
           f"{plan.layout.uplinks} are uplinks and are not touched.",
           "#",
           "# Contains ONLY the MLTs, the ports and the flex-UNI I-SID services.",
           "# Not here, on purpose: everything ZTP set (domain, DNS, syslog, SSH, SNMP,",
           "# mgmt) and the vIST / NNI uplinks.",
           "# Apply after review: enable / configure terminal / <paste> / end"]
    if b.unverified:
        out += ["#", "# UNVERIFIED - no real capture shows these yet; check them on the",
                "# first switch before pasting the rest:"]
        out += [f"#   - {u}" for u in sorted(b.unverified)]
    if b.decisions:
        out += ["#", f"# DECISIONS NEEDED: {len(b.decisions)} VLAN(s) have no I-SID "
                     f"yet - their services are commented out below.",
                f"#   See {switch}.isid-decisions.txt; record the choice under "
                f"isid_conventions.explicit and plan again."]
    if b.problems:
        out += ["#", "# PROBLEMS - read these first:"]
        out += [f"# [!] {p}" for p in b.problems]
    out.append(bar)

    if definitions:
        out += ["", "#", "# MLT CONFIGURATION", "#", *definitions,
                "", "#", "# MLT INTERFACE CONFIGURATION", "#", *interfaces]
    if phase1:
        out += ["", "#", "# PORT CONFIGURATION - PHASE I", "#", *phase1]
    if phase2 or member_lines:
        out += ["", "#", "# PORT CONFIGURATION - PHASE II", "#", *member_lines, *phase2]

    services: list[str] = []
    for isid in sorted(b.services):
        s = b.services[isid]
        services += ["", f"# from {s.source}", f"i-sid {isid} elan"]
        for vlan in sorted(s.cvids):
            services += [f"c-vid {vlan} {t}" for t in _targets(s.cvids[vlan])]
        services += [f"untagged-traffic {t}" for t in _targets(s.untagged)]
        services.append("exit")
    for vlan, (decision, uses) in sorted(b.review.items()):
        cands = " / ".join(map(str, decision.candidates)) or "(no offsets configured)"
        name = f" ({decision.vlan_name})" if decision.vlan_name else ""
        services += ["", f"# [REVIEW] VLAN {vlan}{name}: I-SID undecided - candidates "
                         f"{cands}. Decide, then plan again:", "# i-sid <ISID> elan"]
        tagged = [t for kinds, t in uses if "t" in kinds]
        untagged = [t for kinds, t in uses if "u" in kinds]
        services += [f"# c-vid {vlan} {t}" for t in _targets(tagged)]
        services += [f"# untagged-traffic {t}" for t in _targets(untagged)]
        services.append("# exit")
    if services or b.notes:
        out += ["", "#", "# I-SID CONFIGURATION", "#", *b.notes, *services]
    if not moves:
        out += ["", "# (the plan puts nothing on this switch)"]
    return "\n".join(out).rstrip() + "\n"


def render_all(audits: list[SwitchAudit], plan: MigrationPlan,
               cfg: Config) -> list[NewSwitchConfig]:
    """One config per new switch of the plan."""
    return [render_switch(f.name, plan, audits, cfg) for f in plan.fill]
