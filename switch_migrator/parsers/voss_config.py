"""Parse a VOSS / Fabric Engine `show running-config` into an L2 port model.

`show` output tells you which VLANs are on a port. Only the running-config
tells you *how* they egress it - `c-vid <vlan> port ...` versus
`untagged-traffic port ...` on a flex-UNI leaf, VLAN membership against
`default-vlan-id` on a traditional one. That distinction is what gets
configured on the new switch, so it belongs on the migration sheet, and the
config is the only place it can be read from.

Scope is deliberately narrow: VLANs, I-SIDs, port and MLT membership, tagging.
Everything else in the config - and every secret in it - is ignored here, the
same way `config_extract` ignores it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from switch_migrator.models import BOTH, TAGGED, UNTAGGED, VlanBinding
from switch_migrator.parsers.common import expand_port_list

SOURCE = "running-config"


@dataclass
class FlexService:
    """One I-SID as it attaches to one port or MLT (flex-UNI / switched-UNI)."""
    cvids: set[int] = field(default_factory=set)   # 'c-vid <vid> port ...'
    untagged: bool = False                         # 'untagged-traffic port ...'


@dataclass
class VossConfigModel:
    vlan_names: dict[int, str] = field(default_factory=dict)
    vlan_types: dict[int, str] = field(default_factory=dict)
    vlan_isid: dict[int, int] = field(default_factory=dict)
    vlan_members: dict[int, list[str]] = field(default_factory=dict)
    port_default_vlan: dict[str, int] = field(default_factory=dict)
    port_dot1q: set[str] = field(default_factory=set)
    port_flex_uni: set[str] = field(default_factory=set)
    port_names: dict[str, str] = field(default_factory=dict)
    mlt_members: dict[int, list[str]] = field(default_factory=dict)
    mlt_names: dict[int, str] = field(default_factory=dict)
    # ('port', '1/4') / ('mlt', '197') -> {isid: FlexService}
    flex: dict[tuple[str, str], dict[int, FlexService]] = field(default_factory=dict)

    def service(self, kind: str, target: str, isid: int) -> FlexService:
        return self.flex.setdefault((kind, target), {}).setdefault(isid, FlexService())


def _vlan_ids(spec: str) -> list[int]:
    """'100' / '4051-4052' / '100,200' -> the VLAN ids they name."""
    ids: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            if lo.isdigit() and hi.isdigit() and 0 <= int(hi) - int(lo) <= 4094:
                ids.extend(range(int(lo), int(hi) + 1))
            continue
        if chunk.isdigit():
            ids.append(int(chunk))
    return [v for v in ids if 1 <= v <= 4094]


def parse_voss_config(text: str) -> VossConfigModel:
    """Read the L2 model out of a VOSS running-config.

    Context blocks (`interface GigabitEthernet ...`, `i-sid <n> elan`,
    `interface mlt <n>`) are tracked by their opening line and closed by
    `exit`, so a line like `name "x"` is attributed to the right owner.
    """
    model = VossConfigModel()
    ctx_kind = ""        # '' | 'port' | 'isid' | 'mlt'
    ctx_ports: list[str] = []
    ctx_isid: int | None = None
    ctx_mlt: int | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line == "exit":
            ctx_kind, ctx_ports, ctx_isid, ctx_mlt = "", [], None, None
            continue

        m = re.match(r"interface GigabitEthernet\s+(\S+)", line, re.IGNORECASE)
        if m:
            ctx_kind, ctx_ports = "port", expand_port_list(m.group(1))
            continue

        m = re.match(r"interface mlt\s+(\d+)", line, re.IGNORECASE)
        if m:
            ctx_kind, ctx_mlt = "mlt", int(m.group(1))
            continue

        # 'i-sid 2500695 elan' opens a flex-UNI service block. The bare
        # 'i-sid <n>' form (no keyword) is the same block on some releases.
        m = re.match(r"i-sid\s+(\d+)\b(?!\s*\.)", line, re.IGNORECASE)
        if m and not line.lower().startswith("vlan"):
            ctx_kind, ctx_isid = "isid", int(m.group(1))
            continue

        if ctx_kind == "isid" and ctx_isid is not None:
            _parse_isid_block_line(model, line, ctx_isid)
            continue

        if ctx_kind == "port" and ctx_ports:
            _parse_port_block_line(model, line, ctx_ports)
            continue

        if ctx_kind == "mlt" and ctx_mlt is not None:
            m = re.match(r'name\s+"(.*)"', line)
            if m:
                model.mlt_names[ctx_mlt] = m.group(1)
            continue

        _parse_global_line(model, line)

    return model


def _parse_isid_block_line(model: VossConfigModel, line: str, isid: int) -> None:
    # 'c-vid 695 port 1/4' / 'c-vid 695 mlt 197'
    m = re.match(r"c-vid\s+(\d+)\s+(port|mlt)\s+(\S+)", line, re.IGNORECASE)
    if m:
        vid, kind, target = int(m.group(1)), m.group(2).lower(), m.group(3)
        for tgt in (expand_port_list(target) if kind == "port"
                    else _vlan_ids(target)):
            model.service(kind, str(tgt), isid).cvids.add(vid)
        return
    # 'untagged-traffic port 1/4' - untagged frames on that port land in this
    # I-SID. There is no c-vid for them, which is exactly the state the sheet
    # has to show rather than guess a VLAN for.
    m = re.match(r"untagged-traffic\s+(port|mlt)\s+(\S+)", line, re.IGNORECASE)
    if m:
        kind, target = m.group(1).lower(), m.group(2)
        for tgt in (expand_port_list(target) if kind == "port"
                    else _vlan_ids(target)):
            model.service(kind, str(tgt), isid).untagged = True


def _parse_port_block_line(model: VossConfigModel, line: str,
                           ports: list[str]) -> None:
    m = re.match(r"default-vlan-id\s+(\d+)", line, re.IGNORECASE)
    if m:
        vid = int(m.group(1))
        for p in ports:
            # 0 means 'no port-based VLAN' (the flex-UNI case), not VLAN 0
            if vid:
                model.port_default_vlan[p] = vid
        return
    if re.match(r"encapsulation dot1q", line, re.IGNORECASE):
        model.port_dot1q.update(ports)
        return
    if re.match(r"flex-uni enable", line, re.IGNORECASE):
        model.port_flex_uni.update(ports)
        return
    m = re.match(r'name\s+"(.*)"', line)
    if m:
        for p in ports:
            model.port_names[p] = m.group(1)


def _parse_global_line(model: VossConfigModel, line: str) -> None:
    m = re.match(r'vlan create\s+(\S+)(?:\s+name\s+"([^"]*)")?'
                 r'(?:\s+type\s+(\S+))?', line, re.IGNORECASE)
    if m:
        for vid in _vlan_ids(m.group(1)):
            if m.group(2):
                model.vlan_names[vid] = m.group(2)
            if m.group(3):
                model.vlan_types[vid] = m.group(3)
        return

    m = re.match(r'vlan name\s+(\d+)\s+"(.*)"', line, re.IGNORECASE)
    if m:
        model.vlan_names[int(m.group(1))] = m.group(2)
        return

    m = re.match(r"vlan i-sid\s+(\d+)\s+(\d+)", line, re.IGNORECASE)
    if m:
        model.vlan_isid[int(m.group(1))] = int(m.group(2))
        return

    # 'vlan members add 100 1/1-1/2 portmember' - the operation keyword is
    # optional and defaults to add
    m = re.match(r"vlan members(?:\s+(add|remove))?\s+(\S+)\s+(\S+)",
                 line, re.IGNORECASE)
    if m:
        op = (m.group(1) or "add").lower()
        ports = expand_port_list(m.group(3))
        for vid in _vlan_ids(m.group(2)):
            current = model.vlan_members.setdefault(vid, [])
            if op == "remove":
                model.vlan_members[vid] = [p for p in current if p not in ports]
            else:
                current.extend(p for p in ports if p not in current)
        return

    # 'mlt 197 enable name "srv-lag"' / 'mlt 35 member 1/11,1/12'
    m = re.match(r"mlt\s+(\d+)\s+(.*)", line, re.IGNORECASE)
    if m:
        mlt_id, rest = int(m.group(1)), m.group(2)
        name = re.search(r'name\s+"([^"]*)"', rest)
        if name:
            model.mlt_names[mlt_id] = name.group(1)
        members = re.search(r"member\s+(\S+)", rest)
        if members:
            model.mlt_members[mlt_id] = expand_port_list(members.group(1))


def _flex_bindings(services: dict[int, FlexService]) -> list[VlanBinding]:
    """Turn one target's flex-UNI services into VLAN<->I-SID pairs."""
    out: list[VlanBinding] = []
    for isid, svc in sorted(services.items()):
        if svc.cvids:
            for vid in sorted(svc.cvids):
                # a single c-vid sharing its I-SID with the untagged traffic is
                # one binding that egresses both ways; with several c-vids
                # there is no way to say which one the untagged frames are, so
                # they get their own row instead of being attributed to a guess
                tagging = BOTH if (svc.untagged and len(svc.cvids) == 1) else TAGGED
                out.append(VlanBinding(vlan=vid, isid=isid, tagging=tagging,
                                       source=SOURCE))
            if svc.untagged and len(svc.cvids) > 1:
                out.append(VlanBinding(vlan=None, isid=isid, tagging=UNTAGGED,
                                       source=SOURCE))
        elif svc.untagged:
            out.append(VlanBinding(vlan=None, isid=isid, tagging=UNTAGGED,
                                   source=SOURCE))
    return out


def port_bindings(model: VossConfigModel) -> dict[str, list[VlanBinding]]:
    """Per-port VLAN<->I-SID pairs with tagging, from the config alone."""
    result: dict[str, list[VlanBinding]] = {}
    for (kind, target), services in model.flex.items():
        if kind == "port":
            result.setdefault(target, []).extend(_flex_bindings(services))

    for vid, members in model.vlan_members.items():
        for port in members:
            default = model.port_default_vlan.get(port)
            if default == vid:
                tagging = UNTAGGED
            elif default is not None or port in model.port_dot1q:
                tagging = TAGGED
            else:
                tagging = ""      # nothing in the config says - do not guess
            result.setdefault(port, []).append(VlanBinding(
                vlan=vid, isid=model.vlan_isid.get(vid), tagging=tagging,
                source=SOURCE))
    for bindings in result.values():
        bindings.sort(key=lambda b: (b.vlan is None, b.vlan or 0, b.isid or 0))
    return result


def mlt_bindings(model: VossConfigModel) -> dict[int, list[VlanBinding]]:
    """Per-MLT VLAN<->I-SID pairs the config attaches directly to the MLT."""
    result: dict[int, list[VlanBinding]] = {}
    for (kind, target), services in model.flex.items():
        if kind == "mlt" and target.isdigit():
            result.setdefault(int(target), []).extend(_flex_bindings(services))
    for bindings in result.values():
        bindings.sort(key=lambda b: (b.vlan is None, b.vlan or 0, b.isid or 0))
    return result
