"""Parsers for VOSS / Fabric Engine CLI output (tested against 8.x-9.4.x
formats). All parsers are deliberately tolerant: they anchor on stable tokens
(port IDs, integer IDs, up/down keywords) instead of column positions, because
column layout shifts between VOSS releases.
"""

from __future__ import annotations

import re

from switch_migrator.models import IstState, MltState, PortState, VlanInfo
from switch_migrator.parsers.common import (
    PORT_LIST_RE,
    PORT_RE,
    expand_port_list,
    parse_updown,
)

_UPDOWN_TOKEN = re.compile(r"^(up|down|testing)$", re.IGNORECASE)


def parse_ports(output: str) -> list[PortState]:
    """`show interfaces gigabitEthernet`

    The command prints several sections (Port Interface, Port Name,
    Port Config, ...); only the leading "Port Interface" section is parsed.
    Its data lines start with a port id and carry ADMIN and OPERATE as the
    last two up/down/testing tokens. If the section banner is missing (older
    releases / partial capture) the whole output is scanned instead, keeping
    the first occurrence of each port.
    """
    lines = output.splitlines()
    start, end = 0, len(lines)
    for i, line in enumerate(lines):
        if line.strip().lower() == "port interface":
            start = i + 1
            for j in range(start, len(lines)):
                title = lines[j].strip()
                if re.fullmatch(r"Port [A-Za-z][A-Za-z -]*", title) \
                        and title.lower() != "port interface":
                    end = j
                    break
            break
    ports: list[PortState] = []
    seen: set[str] = set()
    for line in lines[start:end]:
        tokens = line.split()
        if not tokens or not PORT_RE.match(tokens[0]) or tokens[0] in seen:
            continue
        updown = [t for t in tokens if _UPDOWN_TOKEN.match(t)]
        if len(updown) < 2:
            continue
        seen.add(tokens[0])
        ports.append(PortState(
            port=tokens[0],
            description=tokens[2] if len(tokens) > 2 else "",
            admin_up=parse_updown(updown[-2]),
            oper_up=parse_updown(updown[-1]),
        ))
    return ports


def parse_mlt(output: str) -> list[MltState]:
    """`show mlt`

    Anchors: first token is the integer MLT id; the member column is the first
    token that looks like a port list and contains '/' (VOSS ports always have
    slots); type is 'access'/'trunk'; admin/current are the norm/smlt tokens.
    The trailing VLAN IDS column and footer lines like
    '3 out of 8 Total Num of mlt displayed' are ignored.
    """
    mlts: list[MltState] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 3 or not tokens[0].isdigit():
            continue
        mlt_id = int(tokens[0])
        rest = tokens[1:]
        # optional IFINDEX column directly after the id
        if rest and rest[0].isdigit():
            rest = rest[1:]
        if not rest:
            continue
        name = rest[0]
        members: list[str] = []
        for t in rest[1:]:
            if "/" in t and PORT_LIST_RE.match(t):
                members = expand_port_list(t)
                break
        mlt_type = next((t for t in rest if t.lower() in ("access", "trunk")), "")
        states = [t for t in rest if t.lower() in ("norm", "smlt", "ist")]
        # footer guard: a real MLT row shows at least a type, a state or members
        # ('N out of M Total Num of mlt displayed' shows none of them)
        if not mlt_type and not states and not members:
            continue
        mlts.append(MltState(
            mlt_id=mlt_id,
            name=name,
            mlt_type=mlt_type,
            admin=states[0] if states else "",
            current=states[1] if len(states) > 1 else "",
            members=members,
            is_ist="ist" in name.lower() or "ist" in [s.lower() for s in states],
        ))
    return mlts


def parse_port_state(output: str) -> list[PortState]:
    """`show interfaces gigabitEthernet state`

    Preferred port-state source: the table is narrow enough to never wrap.
    Data lines: <port> <up|down> <up|down> <reason|--> <date> <time>
    """
    ports: list[PortState] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 3 or not PORT_RE.match(tokens[0]):
            continue
        admin, oper = parse_updown(tokens[1]), parse_updown(tokens[2])
        if admin is None or oper is None:
            continue
        reason = tokens[3] if len(tokens) > 3 and tokens[3] != "--" else ""
        ports.append(PortState(port=tokens[0], admin_up=admin, oper_up=oper,
                               state_reason=reason))
    return ports


def parse_port_isid(output: str) -> list[dict]:
    """`show interfaces gigabitEthernet i-sid`

    Data lines: <port> <ifindex> <isid> <vlanid|N/A> <c-vid|N/A> <type> ...
    Returns [{"port": str, "isid": int, "vlan": int|None}, ...]; the VLAN is
    taken from VLANID, falling back to C-VID for switched-UNI rows.
    """
    rows: list[dict] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 5 or not PORT_RE.match(tokens[0]) \
                or not tokens[1].isdigit() or not tokens[2].isdigit():
            continue
        vlan = None
        for candidate in (tokens[3], tokens[4]):
            if candidate.isdigit() and 1 <= int(candidate) <= 4094:
                vlan = int(candidate)
                break
        rows.append({"port": tokens[0], "isid": int(tokens[2]), "vlan": vlan})
    return rows


def parse_virtual_ist(output: str) -> IstState | None:
    """`show virtual-ist`

    First data line: <peer-ip> <vlan> <true|false> <up|down>
    """
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) >= 4 and re.match(r"^\d+\.\d+\.\d+\.\d+$", tokens[0]):
            return IstState(
                peer_ip=tokens[0],
                vlan=int(tokens[1]) if tokens[1].isdigit() else None,
                enabled=tokens[2].lower() == "true",
                session_up=parse_updown(tokens[3]),
                raw=line.strip(),
            )
    return None


def parse_vlan_isid(output: str) -> list[VlanInfo]:
    """`show vlan i-sid`

    Lines: <vlan-id> [<i-sid> [<i-sid name>]]
    """
    vlans: list[VlanInfo] = []
    for line in output.splitlines():
        m = re.match(r"^\s*(\d{1,4})\s*(?:(\d{2,9})\s*(.*))?$", line)
        if not m:
            continue
        vlan_id = int(m.group(1))
        if not 1 <= vlan_id <= 4094:
            continue
        isid = int(m.group(2)) if m.group(2) else None
        name = (m.group(3) or "").strip()
        vlans.append(VlanInfo(vlan_id=vlan_id, name=name, isid=isid))
    return vlans


def parse_vlan_basic(output: str) -> dict[int, str]:
    """`show vlan basic` -> {vlan_id: name}. Used to fill in VLAN names.

    Lines: <vlan-id> <name> <type> ...
    """
    result: dict[int, str] = {}
    for line in output.splitlines():
        m = re.match(r"^\s*(\d{1,4})\s+(\S+)\s+\S+", line)
        if m and 1 <= int(m.group(1)) <= 4094:
            result[int(m.group(1))] = m.group(2)
    return result


def parse_isid_local(output: str) -> dict[int, dict]:
    """`show i-sid` on a DvR controller/BEB.

    Returns {isid: {"cvids": set[int], "name": str}}. C-VIDs appear in the
    PORT/MLT INTERFACES columns as 'c<vid>:<port-or-mlt>' tokens.
    """
    result: dict[int, dict] = {}
    current: int | None = None
    for line in output.splitlines():
        tokens = line.split()
        if tokens and re.match(r"^\d{1,9}$", tokens[0]) and len(tokens) >= 2 \
                and tokens[1].upper() in ("ELAN", "ELAN_TR", "E-TREE", "ETREE", "CFM", "IPVPN", "IP-SHORTCUT"):
            current = int(tokens[0])
            entry = result.setdefault(current, {"cvids": set(), "name": ""})
            # last token can be the I-SID name (not a c-vid/port/origin token)
            tail = tokens[-1]
            if tail.upper() not in ("CONFIG", "DISCOVER", "-") and not tail.startswith("c") \
                    and not PORT_RE.match(tail):
                entry["name"] = tail
        if current is None:
            continue
        for m in re.finditer(r"\bc(\d{1,4}):", line):
            result[current]["cvids"].add(int(m.group(1)))
    return result


def parse_dvr_interfaces(output: str) -> list[dict]:
    """`show dvr interfaces` on a DvR controller.

    Data lines: <ip> <mask> <l3isid> <vrfid> <l2isid> <vlan> <gw-ipv4> ...
    Column spacing varies, so rows are matched by shape: an IPv4 first token,
    then somewhere the triple (integer, VLAN-sized integer, IPv4) which is
    (L2ISID, VLAN, GW). Returns [{"l2isid": int, "vlan": int}, ...].
    """
    ipv4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
    rows: list[dict] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 5 or not ipv4.match(tokens[0]):
            continue
        for i in range(1, len(tokens) - 2):
            if tokens[i].isdigit() and tokens[i + 1].isdigit() \
                    and ipv4.match(tokens[i + 2]):
                l2isid, vlan = int(tokens[i]), int(tokens[i + 1])
                if l2isid > 0 and 1 <= vlan <= 4094:
                    rows.append({"l2isid": l2isid, "vlan": vlan})
                    break
    return rows


def parse_isis_spbm_isid(output: str) -> list[dict]:
    """`show isis spbm i-sid all`

    Lines: <isid> <source-name> <b-vlan> <sysid> <type> <host_name>
    Returns [{"isid": int, "type": str, "host": str}, ...]. The VLAN column is
    the B-VLAN, NOT a customer VLAN - intentionally ignored.
    """
    rows: list[dict] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or not re.match(r"^\d{1,9}$", tokens[0]):
            continue
        typ = next((t for t in tokens if t.lower() in ("config", "discover")), "")
        if not typ:
            continue
        host = tokens[-1] if tokens[-1].lower() not in ("config", "discover") else ""
        rows.append({"isid": int(tokens[0]), "type": typ.lower(), "host": host})
    return rows
