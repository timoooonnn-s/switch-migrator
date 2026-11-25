"""The migration plan: where every old link goes (council decisions D2, D10).

Input is what the collection read from the OLD switches; output is, for every link
that moves, a NEW switch and port - and for an aggregation, the new MLT id and name.
The cabling sheet is the rendering of this plan, and the file a human edits: what
they change there is read back as a pin the next time the plan is made.

The rule is the one the planners used by hand (user, round 1 Q2 and round 2):

* **Infrastructure never moves** - uplinks to the core and the IST/vIST peer link are
  replaced by the new switch's own (ZTP's or the user's).
* **Unused ports are left out** and listed, so somebody checks at the rack that
  nothing is plugged in after all. Uncertain ports go in, flagged.
* **Dual-homed** attachments - real MLTs, and old SMLTs across a vIST pair - get ports
  from **port 1 upward, at the same port number on both new peers**, consecutive when
  a side has several links. The MLT id is 4xx of the first port, the same on both
  peers, which SMLT requires.
* **Single-homed** attachments get ports from the **last access port downward**,
  balanced so both peers fill equally.
* The **last N ports are uplinks** and are never planned. When a pair is full the next
  pair on `--new-switch` is used; what fits nowhere is reported, never squeezed in.
* **Deterministic and pinnable**: old switch, then old port, in faceplate order; the
  same input gives the same plan, and a link a human fixed keeps its place.

A server teamed across two switches without LACP (two plain ports) is not detected
automatically: the switch cannot tell it from two servers. Candidates are listed as
hints; pin them by hand (round 2, R5).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from switch_migrator import standard, usage
from switch_migrator.cabling_sheet import CablingRow
from switch_migrator.models import Platform, PortState, SwitchAudit
from switch_migrator.parsers.common import SLOT_PORT_PARTS, port_sort_key

# two new peers; two sides of an aggregation
PAIR = 2

# (old switch, old port): one port anywhere in the scope
PortKey = tuple[str, str]
# what a human fixed in the sheet: (new switch, new port, new MLT id, new MLT name)
Pins = dict[PortKey, tuple[str, str, "int | None", str]]

DUAL = "dual-homed"
SINGLE = "single-homed"

_DEAD = (usage.UNUSED, usage.LIKELY_UNUSED)


@dataclass(frozen=True)
class Layout:
    """One new switch model: ports 1..`ports` on `slot`, the last `uplinks` of
    them reserved for the fabric uplinks (round 2, R1)."""
    ports: int
    uplinks: int
    slot: int = 1

    def __post_init__(self) -> None:
        if self.ports < 1 or self.uplinks < 0 or self.uplinks >= self.ports:
            raise ValueError(f"a new switch needs more ports than uplinks "
                             f"(ports {self.ports}, uplinks {self.uplinks})")

    @property
    def access_ports(self) -> int:
        return self.ports - self.uplinks

    def port(self, number: int) -> str:
        return f"{self.slot}/{number}"

    def number(self, port: str) -> int | None:
        """The port number of `port` on this layout's slot, or None."""
        parts = port.strip().split("/")
        if len(parts) == SLOT_PORT_PARTS and parts[0] == str(self.slot) and parts[1].isdigit():
            return int(parts[1])
        return None


@dataclass
class Link:
    """One old port with something on it that has to move."""
    old_switch: str
    old_port: str
    uid: str = ""
    usage: str = ""
    neighbor: str = ""
    old_mlt_id: int | None = None
    flags: list[str] = field(default_factory=list)
    unit: str = "1"                   # the box it sits on: the ERS stack unit

    @property
    def key(self) -> tuple[str, str]:
        return (self.old_switch, self.old_port)


@dataclass
class Attachment:
    """One device as the new switches will see it: a single link, or the links of
    one aggregation split into the two sides that go to the two new peers."""
    kind: str                         # DUAL or SINGLE
    sides: list[list[Link]]           # DUAL: [side A, side B]; SINGLE: [[link]]
    lag: bool = False                 # becomes an MLT on the new switches
    evidence: str = ""                # why the links belong together

    @property
    def links(self) -> list[Link]:
        return [link for side in self.sides for link in side]

    @property
    def order(self) -> tuple[str, tuple[int, tuple[int, ...], str]]:
        first = min(self.links, key=lambda link: (link.old_switch, port_sort_key(link.old_port)))
        return (first.old_switch, port_sort_key(first.old_port))


@dataclass
class Move:
    """Where one old link goes."""
    link: Link
    new_switch: str
    new_port: str
    new_mlt_id: int | None = None
    new_mlt_name: str = ""
    rule: str = ""                    # which rule placed it
    flags: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        return "; ".join([self.rule, *self.link.flags, *self.flags])


@dataclass
class NotMoved:
    link: Link
    reason: str


@dataclass
class SwitchFill:
    name: str
    peer: str
    access_ports: int
    dual: int = 0                     # ports used from the bottom (dual-homed)
    single: int = 0                   # ports used from the top (single-homed)
    pinned: int = 0

    @property
    def free(self) -> int:
        return self.access_ports - self.dual - self.single - self.pinned


@dataclass
class MigrationPlan:
    layout: Layout
    moves: list[Move] = field(default_factory=list)
    not_moved: list[NotMoved] = field(default_factory=list)
    fill: list[SwitchFill] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    def move_for(self, old_switch: str, old_port: str) -> Move | None:
        return self._by_key().get((old_switch, old_port))

    def _by_key(self) -> dict[tuple[str, str], Move]:
        return {m.link.key: m for m in self.moves}

    @property
    def overflow(self) -> list[NotMoved]:
        return [n for n in self.not_moved if n.reason.startswith(OVERFLOW)]


OVERFLOW = "no room"


# --------------------------------------------------------------------------
# facts -> attachments
# --------------------------------------------------------------------------

def _connected(p: PortState) -> bool:
    if p.usage:
        return p.usage not in _DEAD
    return bool(p.oper_up or p.lldp_neighbor or p.lldp_neighbor_ip or p.macs)


def _link(audit: SwitchAudit, p: PortState) -> Link:
    flags = []
    if p.usage == usage.UNCERTAIN:
        flags.append("uncertain: was down - check a device is really there")
    elif p.usage == usage.DEGRADED:
        flags.append("down leg of a live MLT - check the cable")
    return Link(audit.name, p.port, p.uid, p.usage,
                p.lldp_neighbor or p.lldp_neighbor_ip or p.name, p.mlt_id, flags,
                unit=_unit(audit.platform, p.port))


def old_peers(audits: list[SwitchAudit]) -> dict[str, str]:
    """Old switches that form a vIST/IST pair, both directions.

    Two independent signs, either is enough: the IST peer IP is the other switch's
    host, or the IST MLT's LLDP neighbour is the other switch's name.
    """
    by_host = {a.host: a.name for a in audits}
    names = {a.name for a in audits}
    peers: dict[str, str] = {}
    for a in audits:
        candidates = set()
        if a.ist is not None and a.ist.peer_ip in by_host:
            candidates.add(by_host[a.ist.peer_ip])
        ports = {p.port: p for p in a.ports}
        for mlt in a.mlts:
            if mlt.is_ist:
                candidates |= {ports[m].lldp_neighbor for m in mlt.members
                               if m in ports and ports[m].lldp_neighbor in names}
        candidates.discard(a.name)
        if len(candidates) == 1:
            other = candidates.pop()
            peers[a.name] = other
            peers[other] = a.name
    return peers


def _unit(platform: Platform, port: str) -> str:
    """The physical box a port is on: the stack unit on ERS ('2/7' -> '2'); a
    VOSS chassis is one box."""
    if platform is Platform.ERS and "/" in port:
        return port.split("/", 1)[0]
    return "1"


def _split(links: list[Link]) -> list[list[Link]]:
    """Two sides for the two new peers. Links that already sat on two different
    units (an ERS distributed MLT) keep that split; otherwise they alternate."""
    links = sorted(links, key=lambda link: port_sort_key(link.old_port))
    units = sorted({link.unit for link in links}, key=port_sort_key)
    if len(units) >= PAIR:
        sides: list[list[Link]] = [[], []]
        for link in links:
            sides[units.index(link.unit) % 2].append(link)
        return sides
    return [links[0::2], links[1::2]]


@dataclass
class _Scan:
    """What a pass over the old ports found."""
    out: list[Attachment] = field(default_factory=list)
    not_moved: list[NotMoved] = field(default_factory=list)
    lags: dict[tuple[str, int], list[Link]] = field(default_factory=dict)
    smlt: dict[tuple[str, int], bool] = field(default_factory=dict)


def attachments(audits: list[SwitchAudit]) -> tuple[list[Attachment], list[NotMoved], list[str]]:
    """Every old link that moves, grouped into attachments; what does not move and
    why; and hints a human should look at."""
    scan = _Scan()
    for audit in sorted(audits, key=lambda a: a.name):
        if audit.reachable:
            _scan_audit(scan, audit)
    _attach_lags(scan, old_peers(audits), {a.name for a in audits})
    out = scan.out
    hints = _teaming_hints([a for a in out if a.kind == SINGLE])
    out.sort(key=lambda a: a.order)
    return out, scan.not_moved, hints


def _scan_audit(scan: _Scan, audit: SwitchAudit) -> None:
    """Sort one old switch's ports: not moved, single, or a LAG member."""
    mlts = {m.mlt_id: m for m in audit.mlts}
    for p in sorted(audit.ports, key=lambda x: port_sort_key(x.port)):
        mlt = mlts.get(p.mlt_id) if p.mlt_id is not None else None
        link = _link(audit, p)
        if mlt is not None and mlt.is_ist:
            scan.not_moved.append(NotMoved(link, "vIST/IST peer link - the new pair has its own"))
            continue
        if p.is_uplink or (mlt is not None and mlt.is_uplink):
            scan.not_moved.append(NotMoved(link, "uplink to the core - the new switch has its own"))
            continue
        if not _connected(p):
            if p.usage or p.oper_up is not None:
                scan.not_moved.append(NotMoved(
                    link, f"unused ({p.usage or 'down, nothing learned'}) - "
                          f"check at the rack that nothing is plugged in"))
            continue
        if mlt is not None:
            scan.lags.setdefault((audit.name, mlt.mlt_id), []).append(link)
            scan.smlt[(audit.name, mlt.mlt_id)] = "smlt" in (
                mlt.admin.lower(), mlt.current.lower())
        else:
            scan.out.append(Attachment(SINGLE, [[link]]))


def _attach_lags(scan: _Scan, peers: dict[str, str], in_run: set[str]) -> None:
    """One attachment per old LAG; the two legs of an SMLT become one."""
    lags = scan.lags
    done: set[tuple[str, int]] = set()
    for (switch, mlt_id), links in sorted(lags.items()):
        if (switch, mlt_id) in done:
            continue
        done.add((switch, mlt_id))
        peer = peers.get(switch)
        smlt = scan.smlt[(switch, mlt_id)]
        if smlt and peer and (peer, mlt_id) in lags:
            done.add((peer, mlt_id))
            first, second = sorted([switch, peer])
            sides = [sorted(lags[(first, mlt_id)], key=lambda link: port_sort_key(link.old_port)),
                     sorted(lags[(second, mlt_id)], key=lambda link: port_sort_key(link.old_port))]
            scan.out.append(Attachment(DUAL, sides, lag=True,
                                       evidence=f"SMLT {mlt_id} on {first} + {second}"))
            continue
        if len(links) == 1:
            link = links[0]
            if smlt and peer and peer not in in_run:
                link.flags.append(f"other SMLT leg is on {peer}, which is not in this run")
            elif smlt and not peer:
                link.flags.append(f"was SMLT {mlt_id}: the other leg's switch is not "
                                  f"in this run - planned as a plain port")
            else:
                link.flags.append(f"was MLT {mlt_id} with one link - planned as a plain port")
            scan.out.append(Attachment(SINGLE, [[link]]))
            continue
        scan.out.append(Attachment(DUAL, _split(links), lag=True,
                                   evidence=f"MLT {mlt_id} on {switch}"))


def _teaming_hints(singles: list[Attachment]) -> list[str]:
    """The same neighbour name on two plain ports of different old switches or
    units may be ONE server teamed without LACP (R5) - or two servers with the
    same NIC model, which is why this is never acted on."""
    by_name: dict[str, list[Link]] = {}
    for att in singles:
        link = att.links[0]
        if link.neighbor:
            by_name.setdefault(link.neighbor, []).append(link)
    out = []
    for name, links in sorted(by_name.items()):
        boxes = {(link.old_switch, link.unit) for link in links}
        if len(links) == PAIR and len(boxes) == PAIR:
            where = " and ".join(f"{link.old_switch} {link.old_port}" for link in links)
            out.append(f"'{name}' is on {where}: if that is one server teamed across "
                       f"two switches, give both the same NEW port on the two peers "
                       f"in the sheet and plan again")
    return out


# --------------------------------------------------------------------------
# attachments -> ports
# --------------------------------------------------------------------------

class _Switch:
    def __init__(self, name: str, peer: str, layout: Layout):
        self.fill = SwitchFill(name, peer, layout.access_ports)
        self.used: set[int] = set()
        self.access = layout.access_ports

    def free(self, number: int) -> bool:
        return 1 <= number <= self.access and number not in self.used


def _pairs(names: list[str]) -> list[tuple[str, str | None]]:
    return [(names[i], names[i + 1] if i + 1 < len(names) else None)
            for i in range(0, len(names), 2)]


def allocate(atts: list[Attachment], new_switches: list[str], layout: Layout,
             mlt_name: str = standard.DEFAULT_MLT_NAME,
             pins: Pins | None = None
             ) -> MigrationPlan:
    """Place every attachment. `pins` is {(old switch, old port): (new switch,
    new port, new MLT id or None, new MLT name)} from a sheet a human edited."""
    plan = MigrationPlan(layout=layout)
    pins = pins or {}
    switches: dict[str, _Switch] = {}
    for a, b in _pairs(new_switches):
        switches[a] = _Switch(a, b or "", layout)
        if b:
            switches[b] = _Switch(b, a, layout)

    remaining = _apply_pins(atts, pins, switches, layout, plan)
    for att in [a for a in remaining if a.kind == DUAL]:
        _place_dual(att, switches, new_switches, mlt_name, plan)
    singles = [a for a in remaining if a.kind == SINGLE]
    for att in singles:
        _place_single(att, switches, new_switches, plan)

    plan.fill = [s.fill for s in switches.values()]
    plan.moves.sort(key=lambda m: (m.link.old_switch, port_sort_key(m.link.old_port)))
    if plan.overflow:
        plan.problems.append(
            f"{len(plan.overflow)} link(s) did not fit on {', '.join(new_switches)} - "
            f"add another pair to --new-switch, or free ports by hand")
    return plan


def _apply_pins(atts: list[Attachment], pins: Pins, switches: dict[str, _Switch],
                layout: Layout, plan: MigrationPlan) -> list[Attachment]:
    remaining = []
    for att in atts:
        pinned = [link for link in att.links if link.key in pins]
        if not pinned:
            remaining.append(att)
            continue
        if len(pinned) < len(att.links):
            loose = [link for link in att.links if link.key not in pins]
            plan.problems.append(
                f"{', '.join(f'{link.old_switch} {link.old_port}' for link in pinned)} "
                f"pinned, but {', '.join(f'{link.old_switch} {link.old_port}' for link in loose)} "
                f"of the same {att.evidence or 'attachment'} not - place those by hand")
            for link in loose:
                plan.not_moved.append(NotMoved(link, "part of a partly pinned aggregation - "
                                                     "place it by hand"))
        for link in pinned:
            new_switch, new_port, mlt_id, name = pins[link.key]
            move = Move(link, new_switch, new_port, mlt_id, name, rule="pinned in the sheet")
            if att.lag and mlt_id is None:
                move.flags.append("member of an aggregation, but no NEW MLT ID in the sheet")
            switch = switches.get(new_switch)
            number = layout.number(new_port)
            if switch is not None and number is not None:
                if number in switch.used:
                    move.flags.append(f"{new_switch} {new_port} is planned twice")
                    plan.problems.append(f"{new_switch} {new_port} is planned twice")
                if number > layout.access_ports:
                    move.flags.append(f"{new_port} is an uplink port on this layout")
                switch.used.add(number)
                switch.fill.pinned += 1
            plan.moves.append(move)
    return remaining


def _place_dual(att: Attachment, switches: dict[str, _Switch], order: list[str],
                mlt_name: str, plan: MigrationPlan) -> None:
    layout = plan.layout
    n = max(len(att.sides[0]), len(att.sides[1]))
    for a, b in _pairs(order):
        if b is None:
            continue
        sa, sb = switches[a], switches[b]
        start = next((p for p in range(1, layout.access_ports - n + 2)
                      if all(sa.free(p + i) and sb.free(p + i) for i in range(n))), None)
        if start is None:
            continue
        mlt_id, name, flags = None, "", []
        if att.lag:
            try:
                mlt_id = standard.mlt_id_for_new_lag(layout.port(start))
                name = standard.mlt_name_for_new_lag(layout.port(start), mlt_name)
            except standard.ConventionError as exc:
                flags.append(f"set the MLT id by hand: {exc}")
        if len(att.sides[0]) != len(att.sides[1]):
            flags.append(f"uneven: {len(att.sides[0])} link(s) on {a}, "
                         f"{len(att.sides[1])} on {b}")
        rule = (f"dual-homed ({att.evidence})" if att.evidence else "dual-homed")
        for switch, side in ((sa, att.sides[0]), (sb, att.sides[1])):
            for i, link in enumerate(side):
                switch.used.add(start + i)
                switch.fill.dual += 1
                plan.moves.append(Move(link, switch.fill.name, layout.port(start + i),
                                       mlt_id, name, rule, list(flags)))
        return
    for link in att.links:
        plan.not_moved.append(NotMoved(link, f"{OVERFLOW}: dual-homed, {n} port(s) "
                                             f"needed on both peers"))


def _place_single(att: Attachment, switches: dict[str, _Switch], order: list[str],
                  plan: MigrationPlan) -> None:
    link = att.links[0]
    for a, b in _pairs(order):
        # balance inside the pair: the switch with fewer single-homed links first
        candidates = [switches[a]] + ([switches[b]] if b else [])
        candidates.sort(key=lambda s: (s.fill.single, order.index(s.fill.name)))
        for switch in candidates:
            number = next((p for p in range(switch.access, 0, -1) if switch.free(p)), None)
            if number is None:
                continue
            switch.used.add(number)
            switch.fill.single += 1
            plan.moves.append(Move(link, switch.fill.name,
                                   f"{plan.layout.slot}/{number}", rule="single-homed"))
            return
    plan.not_moved.append(NotMoved(link, f"{OVERFLOW}: single-homed"))


# --------------------------------------------------------------------------

def make_plan(audits: list[SwitchAudit], new_switches: list[str], layout: Layout,
              mlt_name: str = standard.DEFAULT_MLT_NAME,
              pins: Pins | None = None) -> MigrationPlan:
    """The whole plan from the collected old switches."""
    if not new_switches:
        raise ValueError("a plan needs the new switch names (--new-switch A,B)")
    atts, not_moved, hints = attachments(audits)
    plan = allocate(atts, new_switches, layout, mlt_name, pins)
    plan.not_moved = not_moved + plan.not_moved
    plan.hints = hints
    if len(new_switches) % 2:
        plan.problems.append(
            f"{new_switches[-1]} has no peer on --new-switch: dual-homed links "
            f"never go there")
    return plan


def uids_from_sheet(rows: Iterable[CablingRow]) -> dict[PortKey, str]:
    """The Port IDs an earlier sheet gave, so a re-plan keeps them."""
    return {(r.old_switch, r.old_port): r.uid for r in rows if r.uid}


def pins_from_sheet(rows: Iterable[CablingRow]) -> Pins:
    """The NEW entries of an edited cabling sheet, as pins for the next plan."""
    return {(r.old_switch, r.old_port): (r.new_switch, r.new_port, r.new_mlt_id,
                                         r.new_mlt_name)
            for r in rows if r.new_switch and r.new_port}
