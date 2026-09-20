"""Parse an ERS/BOSS `show running-config` into a small L2 model.

Only what the VOSS flex-UNI generator needs: VLANs (+names+members), per-port
tagging / PVID / name / admin state, and MLTs. Everything else is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from switch_migrator.models import BOTH, TAGGED, UNTAGGED, VlanBinding
from switch_migrator.parsers.common import expand_port_list


# 'vlan ports X tagging <mode>'. ERS spells the four combinations out:
#   untagAll       every frame egresses untagged (the default)
#   tagAll         every frame egresses tagged
#   untagPvidOnly  the PVID egresses untagged, everything else tagged
#                  - the ordinary trunk-with-a-native-VLAN
#   tagPvidOnly    the inverse, and rare
UNTAG_ALL = "untagAll"
TAG_ALL = "tagAll"
UNTAG_PVID_ONLY = "untagPvidOnly"
TAG_PVID_ONLY = "tagPvidOnly"
_TAGGING_MODES = {m.lower(): m for m in
                  (UNTAG_ALL, TAG_ALL, UNTAG_PVID_ONLY, TAG_PVID_ONLY)}


@dataclass
class ErsPort:
    port: str                 # ERS unit port number as a string, e.g. "7"
    name: str = ""
    tagging_mode: str = UNTAG_ALL   # the port's 'vlan ports ... tagging' mode
    pvid: int | None = None   # untagged/native VLAN
    shutdown: bool = False

    @property
    def tagged(self) -> bool:
        """Does this port egress tagged at all? (kept for the VOSS generator,
        which only needs the tagAll/not distinction it always used.)"""
        return self.tagging_mode == TAG_ALL


@dataclass
class ErsVlan:
    vlan_id: int
    name: str = ""
    members: list[str] = field(default_factory=list)


@dataclass
class ErsMlt:
    mlt_id: int
    name: str = ""
    members: list[str] = field(default_factory=list)


@dataclass
class ErsModel:
    vlans: dict[int, ErsVlan] = field(default_factory=dict)
    ports: dict[str, ErsPort] = field(default_factory=dict)
    mlts: list[ErsMlt] = field(default_factory=list)

    def port(self, p: str) -> ErsPort:
        return self.ports.setdefault(p, ErsPort(port=p))

    def vlan(self, vid: int) -> ErsVlan:
        return self.vlans.setdefault(vid, ErsVlan(vlan_id=vid))


def _ids(spec: str) -> list[int]:
    return [int(x) for x in expand_port_list(spec)]


def parse_ers_config(text: str) -> ErsModel:
    m = ErsModel()
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("!"):
            continue

        mm = re.match(r"vlan create\s+(\S+)\s+type", s)
        if mm:
            for vid in _ids(mm.group(1)):
                m.vlan(vid)
            continue

        mm = re.match(r'vlan name\s+(\d+)\s+"(.*)"', s)
        if mm:
            m.vlan(int(mm.group(1))).name = mm.group(2)
            continue

        mm = re.match(r"vlan members(?:\s+(add|remove))?\s+(\S+)\s+(\S+)", s)
        if mm:
            op, idspec, portspec = mm.groups()
            ports = [] if portspec.upper() == "NONE" else expand_port_list(portspec)
            for vid in _ids(idspec):
                vlan = m.vlan(vid)
                if op == "remove":
                    vlan.members = [p for p in vlan.members if p not in ports]
                else:
                    vlan.members += [p for p in ports if p not in vlan.members]
            continue

        mm = re.match(r"vlan ports\s+(\S+)\s+tagging\s+(\S+)", s)
        if mm and mm.group(2).lower() in _TAGGING_MODES:
            mode = _TAGGING_MODES[mm.group(2).lower()]
            for p in expand_port_list(mm.group(1)):
                m.port(p).tagging_mode = mode
            continue

        mm = re.match(r"vlan ports\s+(\S+)\s+pvid\s+(\d+)", s)
        if mm:
            for p in expand_port_list(mm.group(1)):
                m.port(p).pvid = int(mm.group(2))
            continue

        # port ids are flat on a standalone ('5') and unit-qualified on a
        # stack ('1/5') - both forms appear in the same commands
        mm = re.match(r'name port\s+(\S+)\s+"(.*)"', s)
        if mm:
            for p in expand_port_list(mm.group(1)):
                m.port(p).name = mm.group(2)
            continue

        mm = re.match(r"shutdown port\s+(\S+)", s)
        if mm:
            for p in expand_port_list(mm.group(1)):
                m.port(p).shutdown = True
            continue

        # 'mlt 1 name "x" enable member 49-50' - skip 'mlt spanning-tree ...'
        mm = re.match(r"mlt\s+(\d+)\b(.*)", s)
        if mm and "member" in mm.group(2):
            rest = mm.group(2)
            name_m = re.search(r'name\s+"([^"]*)"', rest)
            mem_m = re.search(r"member\s+(\S+)", rest)
            m.mlts.append(ErsMlt(
                mlt_id=int(mm.group(1)),
                name=name_m.group(1) if name_m else "",
                members=expand_port_list(mem_m.group(1)) if mem_m else []))
            continue
    return m


SOURCE = "running-config"


def port_bindings(model: ErsModel) -> dict[str, list[VlanBinding]]:
    """Per-port VLAN bindings with tagging, from an ERS running-config.

    ERS has no I-SIDs, so every binding leaves `isid` unset - it is filled in
    later from the fabric comparison, which is the only thing that knows which
    I-SID an ERS VLAN lands on. Tagging comes from the port's
    `vlan ports ... tagging` mode read against its PVID, so the ordinary
    trunk-with-a-native-VLAN (`untagPvidOnly`) comes out as one untagged VLAN
    and the rest tagged - not, as a plain tagAll/not test would have it, as a
    wholly untagged port.

    A port the config never mentions in a `vlan ports ... tagging` line is
    untagAll by default on ERS, which is why absence is read as untagged
    rather than as unknown.
    """
    result: dict[str, list[VlanBinding]] = {}
    for vid, vlan in sorted(model.vlans.items()):
        for port in vlan.members:
            cfg = model.ports.get(port)
            mode = cfg.tagging_mode if cfg else UNTAG_ALL
            is_pvid = cfg is not None and cfg.pvid == vid
            if mode == TAG_ALL:
                # everything egresses tagged; the PVID additionally takes the
                # port's untagged ingress
                tagging = BOTH if is_pvid else TAGGED
            elif mode == UNTAG_ALL:
                tagging = UNTAGGED
            elif mode == UNTAG_PVID_ONLY:
                tagging = UNTAGGED if is_pvid else TAGGED
            else:                                    # tagPvidOnly
                tagging = TAGGED if is_pvid else UNTAGGED
            result.setdefault(port, []).append(
                VlanBinding(vlan=vid, tagging=tagging, source=SOURCE))
    for bindings in result.values():
        bindings.sort(key=lambda b: (b.vlan is None, b.vlan or 0))
    return result
