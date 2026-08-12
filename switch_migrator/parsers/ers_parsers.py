"""Parsers for ERS / BOSS CLI output (4x00/5x00 stackables).

Same philosophy as the VOSS parsers: anchor on stable tokens, not columns.
"""

from __future__ import annotations

import re

from switch_migrator.models import IstState, MltState, PortState, VlanInfo
from switch_migrator.parsers.common import PORT_RE, expand_port_list, name_says_ist

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

    Header line: <id> <name (may contain spaces)> <type> ... - the type keyword
    anchors the end of the name column. Many BOSS/ERS releases print an
    indented 'Port Members: <list>' continuation line under each VLAN (real
    5900 output); when present it is attached to the VLAN above. 'NONE' means
    no ports.
    """
    vlans: list[VlanInfo] = []
    type_re = re.compile(
        r"^\s*(\d{1,4})\s+(.*?)\s+(" + "|".join(re.escape(t) for t in _VLAN_TYPES) + r")\b",
        re.IGNORECASE,
    )
    member_re = re.compile(r"Port\s+Members?\s*:\s*(.+?)\s*$", re.IGNORECASE)
    current: VlanInfo | None = None
    for line in output.splitlines():
        m = type_re.match(line)
        if m:
            vlan_id = int(m.group(1))
            if not 1 <= vlan_id <= 4094:
                current = None
                continue
            current = VlanInfo(vlan_id=vlan_id, name=m.group(2).strip())
            vlans.append(current)
            continue
        pm = member_re.search(line)
        if pm and current is not None:
            raw = pm.group(1).strip()
            current.members = [] if raw.upper() == "NONE" else expand_port_list(raw)
    return vlans


def parse_mlt(output: str) -> list[MltState]:
    """`show mlt`

    Column layouts seen on real gear:
      classic: <id> <name> <members|NONE> <bpdu> <mode> <status> <type>
      59xx:    <id> <name> <members|NONE> <bpdu> <mode> <status> [<type>] <key>
    The 59xx LACP variant appends a KEY column and leaves TYPE empty on
    unconfigured slots, so nothing is at a fixed position from the right.
    Rows are anchored instead on the Enabled/Disabled STATUS token (searched
    from the right) with members exactly three tokens before it.

    Unconfigured trunk slots - Disabled with no members, and the 59xx prints
    all 64 of them ('Trunk #7   NONE ... Disabled') - are dropped entirely:
    there is nothing to migrate and they must not be reported as dead MLTs.
    An ENABLED trunk without members is kept (genuinely dead leftover).
    """
    mlts: list[MltState] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 6 or not tokens[0].isdigit():
            continue
        status_idx = next((i for i in range(len(tokens) - 1, 0, -1)
                           if tokens[i].lower() in ("enabled", "disabled")), None)
        # need at least <name> <members> <bpdu> <mode> between the id and STATUS
        if status_idx is None or status_idx < 4:
            continue
        members_tok = tokens[status_idx - 3]
        if members_tok.upper() != "NONE" and not re.match(r"^[\d/,\-]+$", members_tok):
            continue
        status = tokens[status_idx]
        members = expand_port_list(members_tok)
        if not members and status.lower() == "disabled":
            continue  # unconfigured slot, not a dead MLT
        name = " ".join(tokens[1:status_idx - 3])
        after = tokens[status_idx + 1:]
        mlt_type = after[0] if after and after[0].upper() != "NONE" \
            and not after[0].isdigit() else ""
        mlts.append(MltState(
            mlt_id=int(tokens[0]),
            name=name,
            mlt_type=mlt_type,
            admin=status,
            members=members,
            is_ist=name_says_ist(name),
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
