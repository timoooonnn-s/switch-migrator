"""Parse an ERS/BOSS `show running-config` into a small L2 model.

Only what the VOSS flex-UNI generator needs: VLANs (+names+members), per-port
tagging / PVID / name / admin state, and MLTs. Everything else is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from switch_migrator.parsers.common import expand_port_list


@dataclass
class ErsPort:
    port: str                 # ERS unit port number as a string, e.g. "7"
    name: str = ""
    tagged: bool = False      # 'vlan ports X tagging tagAll' -> 802.1Q trunk
    pvid: int | None = None   # untagged/native VLAN
    shutdown: bool = False


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

        mm = re.match(r"vlan ports\s+(\S+)\s+tagging\s+tagAll", s)
        if mm:
            for p in expand_port_list(mm.group(1)):
                m.port(p).tagged = True
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
