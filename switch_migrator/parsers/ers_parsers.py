"""Parsers for ERS / BOSS CLI output (4x00/5x00 stackables).

Same philosophy as the VOSS parsers: anchor on stable tokens, not columns.
"""

from __future__ import annotations

import re

from switch_migrator.models import IstState, MltState, PortState, VlanInfo
from switch_migrator.parsers.common import PORT_RE, expand_port_list

_VLAN_TYPES = ("Port", "Protocol", "Protocol-based", "MAC", "MACSA", "SPBM-BVLAN",
               "Spbm-bvlan", "Private", "IDS", "RSPAN")


def parse_ports(output: str) -> list[PortState]:
    """`show interfaces`

    Data lines: <port> [<trunk>] Enable|Disable Up|Down Up|Down ...
    ADMIN is the Enable/Disable token, OPER is the first Up/Down after it.
    """
    ports: list[PortState] = []
    for line in output.splitlines():
        tokens = line.split()
        if not tokens or not PORT_RE.match(tokens[0]):
            continue
        admin_idx = next((i for i, t in enumerate(tokens)
                          if t.lower() in ("enable", "disable")), None)
        if admin_idx is None:
            continue
        oper = next((t for t in tokens[admin_idx + 1:] if t.lower() in ("up", "down")), None)
        if oper is None:
            continue
        ports.append(PortState(
            port=tokens[0],
            admin_up=tokens[admin_idx].lower() == "enable",
            oper_up=oper.lower() == "up",
        ))
    return ports


def parse_vlans(output: str) -> list[VlanInfo]:
    """`show vlan`

    Lines: <id> <name (may contain spaces)> <type> ... - the type keyword
    anchors the end of the name column.
    """
    vlans: list[VlanInfo] = []
    type_re = re.compile(
        r"^\s*(\d{1,4})\s+(.*?)\s+(" + "|".join(re.escape(t) for t in _VLAN_TYPES) + r")\b",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        m = type_re.match(line)
        if not m:
            continue
        vlan_id = int(m.group(1))
        if not 1 <= vlan_id <= 4094:
            continue
        vlans.append(VlanInfo(vlan_id=vlan_id, name=m.group(2).strip()))
    return vlans


def parse_mlt(output: str) -> list[MltState]:
    """`show mlt`

    Lines: <id> <name (16 chars, may be empty/contain spaces)> <members|NONE>
           <bpdu> <mode> Enabled|Disabled <type>
    Parsed right-anchored: type, status, mode, bpdu, members are the last five
    tokens; everything between id and members is the name.
    """
    mlts: list[MltState] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 6 or not tokens[0].isdigit():
            continue
        status = tokens[-2]
        if status.lower() not in ("enabled", "disabled"):
            continue
        members_tok = tokens[-5]
        if members_tok.upper() != "NONE" and not re.match(r"^[\d/,\-]+$", members_tok):
            continue
        name = " ".join(tokens[1:-5])
        mlts.append(MltState(
            mlt_id=int(tokens[0]),
            name=name,
            mlt_type=tokens[-1],
            admin=status,
            members=expand_port_list(members_tok),
            is_ist="ist" in name.lower(),
        ))
    return mlts


def parse_ist(output: str) -> IstState | None:
    """`show ist` - key/value style output, wording differs per BOSS release."""
    ist = IstState(raw=output.strip()[:500])
    found = False
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = re.sub(r"[^a-z]", "", key.lower())
        value = value.strip()
        if key.endswith(("enable", "enabled")):
            ist.enabled = value.lower() in ("true", "enabled", "enable", "yes")
            found = True
        elif "peerip" in key or key == "peer":
            ist.peer_ip = value
            found = True
        elif "vlan" in key and value.split():
            first = value.split()[0]
            if first.isdigit():
                ist.vlan = int(first)
                found = True
        elif "status" in key:
            ist.session_up = value.lower() in ("up", "true", "established")
            found = True
    return ist if found else None
