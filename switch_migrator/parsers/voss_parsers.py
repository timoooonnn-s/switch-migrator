"""Parsers for VOSS / Fabric Engine CLI output (tested against 8.x-9.4.x
formats). All parsers are deliberately tolerant: they anchor on stable tokens
(port IDs, integer IDs, up/down keywords) instead of column positions, because
column layout shifts between VOSS releases.
"""

from __future__ import annotations

import re

from switch_migrator.models import IstState, MltState, PortState, VlanInfo
from switch_migrator.parsers.common import (
    PORT_RE,
    expand_port_list,
    is_port_list,
    name_says_ist,
    parse_updown,
)

_UPDOWN_TOKEN = re.compile(r"^(up|down|testing)$", re.IGNORECASE)


_MAC_TOKEN = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")


def _description(tokens: list[str]) -> str:
    """DESCRIPTION column of the Port Interface table ('10GbSR', '1000BaseTX').

    It is the third column, but it is blank on ports with no media, which
    shifts every following column left - so the token is only taken when it
    actually looks like a description and not like the LINK TRAP / MTU / MAC
    columns that slide into its place.
    """
    if len(tokens) <= 2:
        return ""
    tok = tokens[2]
    if tok.lower() in ("true", "false") or tok.isdigit() or _MAC_TOKEN.match(tok):
        return ""
    return tok


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
            description=_description(tokens),
            admin_up=parse_updown(updown[-2]),
            oper_up=parse_updown(updown[-1]),
        ))
    return ports


def parse_mlt(output: str) -> list[MltState]:
    """`show mlt`

    The plain command prints FOUR tables (Mlt Info, LACP, local/remote port
    members, ENCAP) plus 'All N out of M ...' footers, and the trailing VLAN
    IDS column wraps onto continuation lines for long VLAN lists. Only the
    first table carries what we need, so rows are accepted only when they
    show a TYPE (access/trunk) or ADMIN/CURRENT state (norm/smlt) token -
    which the other tables, footers and wrapped continuation lines never do -
    and each MLT id is kept once (first occurrence wins, Mlt Info comes
    first). Member ports are the first port-list token containing '/'.

    The third table ("WHICH PORTS PROGRAMMED IN DATA PATH") is parsed too and
    used to set `in_datapath` on each MLT: LOCAL / REMOTE => the MLT has ports
    forwarding, NONE => it does not. This is the only member-liveness signal we
    have on boxes that reject every `show interfaces gigabitEthernet` variant.
    """
    datapath = _parse_mlt_datapath(output)
    mlts: list[MltState] = []
    seen: set[int] = set()
    current: MltState | None = None   # last Mlt Info row, for VLAN continuations
    for line in output.splitlines():
        tokens = line.split()
        # a line of bare VLAN ids continues the previous row's VLAN IDS column
        if (current is not None and tokens
                and all(t.isdigit() and 1 <= int(t) <= 4094 for t in tokens)):
            current.vlans.extend(int(t) for t in tokens
                                 if int(t) not in current.vlans)
            continue
        current = None
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
        members_at = None
        for i, t in enumerate(rest[1:], start=1):
            if "/" in t and is_port_list(t):
                members = expand_port_list(t)
                members_at = i
                break
        mlt_type = next((t for t in rest if t.lower() in ("access", "trunk")), "")
        states = [t for t in rest if t.lower() in ("norm", "smlt", "ist")]
        if (not mlt_type and not states) or mlt_id in seen:
            continue
        seen.add(mlt_id)
        # everything after the PORT MEMBERS column is the VLAN IDS list
        vlans = ([int(t) for t in rest[members_at + 1:]
                  if t.isdigit() and 1 <= int(t) <= 4094]
                 if members_at is not None else [])
        mlt = MltState(
            mlt_id=mlt_id,
            name=name,
            mlt_type=mlt_type,
            admin=states[0] if states else "",
            current=states[1] if len(states) > 1 else "",
            members=members,
            vlans=vlans,
            in_datapath=datapath.get(mlt_id),
            is_ist=name_says_ist(name) or "ist" in [s.lower() for s in states],
        )
        mlts.append(mlt)
        current = mlt
    return mlts


def _is_header_line(line: str) -> bool:
    """A column-title line: text, no leading id, not a rule or a footer."""
    stripped = line.strip()
    if not stripped or set(stripped) <= set("-=+ "):
        return False
    if "out of" in stripped.lower():
        return False
    return not stripped.split()[0].isdigit()


def parse_mlt_lacp(output: str) -> dict[int, bool]:
    """Parse the LACP table of `show mlt` -> {mlt_id: lacp_admin_enabled}.

    Second table of the plain `show mlt` output, whose header spans two lines:
                     DESIGNATED   LACP      LACP
        MLTID IFINDEX  PORTS      ADMIN     OPER
        197   6340     1/43       enable    up

    The table is identified by looking BACKWARDS from the 'MLTID' line over the
    title lines directly above it - never by remembering that the word 'LACP'
    appeared somewhere earlier. That distinction matters: an MLT *named*
    something with LACP in it appears as a data row in the Mlt Info and
    data-path tables, and a forward-looking latch would then mistake the next
    table (ENCAP DOT1Q, whose column is also enable/disable) for this one and
    silently record the DOT1Q state as the LACP state.
    """
    lines = output.splitlines()
    result: dict[int, bool] = {}
    in_section = False
    for i, line in enumerate(lines):
        upper = line.upper()
        if "MLTID" in upper and _is_header_line(line):
            header = upper
            for j in range(i - 1, max(i - 4, -1), -1):
                if not _is_header_line(lines[j]):
                    break
                header = lines[j].upper() + " " + header
            # both words are needed: the Mlt Info header also carries ADMIN,
            # the data-path header neither
            in_section = "LACP" in header and "ADMIN" in header
            continue
        if not in_section:
            continue
        if "OUT OF" in upper and "TOTAL" in upper:
            in_section = False
            continue
        tokens = line.split()
        if not tokens or not tokens[0].isdigit():
            continue
        admin = next((t.lower() for t in tokens
                      if t.lower() in ("enable", "disable",
                                       "enabled", "disabled")), None)
        if admin is not None:
            result[int(tokens[0])] = admin.startswith("enable")
    return result


def _parse_mlt_datapath(output: str) -> dict[int, bool]:
    """Parse the "WHICH PORTS PROGRAMMED IN DATA PATH" table of `show mlt`.

    Returns {mlt_id: is_programmed}. The last field of every data row is the
    data-path state (LOCAL / REMOTE / 'LOCAL & REMOTE' / NONE); LOCAL or REMOTE
    means the MLT is forwarding on at least one local/remote member, NONE means
    it is not. Only rows inside this one table are read - the section is entered
    on the 'IN DATA PATH' header and left on the next 'out of ... Total' footer
    or the next table banner - so the LACP (up/down) and ENCAP (enable/disable)
    tables can never be misread as data-path state.
    """
    result: dict[int, bool] = {}
    in_section = False
    for line in output.splitlines():
        upper = line.upper()
        if "IN DATA PATH" in upper:
            in_section = True
            continue
        if not in_section:
            continue
        if "OUT OF" in upper and "TOTAL" in upper:
            in_section = False
            continue
        tokens = line.split()
        if not tokens or not tokens[0].isdigit():
            continue
        last = tokens[-1].upper()
        if last in ("LOCAL", "REMOTE"):        # 'LOCAL & REMOTE' ends in REMOTE
            result[int(tokens[0])] = True
        elif last == "NONE":
            result[int(tokens[0])] = False
    return result


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
        # trailing DATE column = when the port last changed state. Free, and
        # the single best evidence for "is this port actually used": down since
        # May is decommissioned, down since 10 minutes ago is a link flap.
        last_change = ""
        for i, t in enumerate(tokens[3:], start=3):
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", t):
                last_change = t + (f" {tokens[i + 1]}" if i + 1 < len(tokens)
                                   and re.fullmatch(r"\d{1,2}:\d{2}:\d{2}",
                                                    tokens[i + 1]) else "")
                break
        ports.append(PortState(port=tokens[0], admin_up=admin, oper_up=oper,
                               state_reason=reason, last_change=last_change))
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
        # anchored with \s+ separators so numeric footer lines
        # ('3 out of 3 Total Num of Vlans displayed') can never match
        m = re.match(r"^\s*(\d{1,4})(?:\s+(\d{1,9})(?:\s+(\S.*?))?)?\s*$", line)
        if not m:
            continue
        vlan_id = int(m.group(1))
        if not 1 <= vlan_id <= 4094:
            continue
        isid = int(m.group(2)) if m.group(2) else None
        # the third column is the I-SID NAME, not the VLAN name - keep them
        # separate so the report can show both
        isid_name = (m.group(3) or "").strip()
        vlans.append(VlanInfo(vlan_id=vlan_id, isid=isid, isid_name=isid_name))
    return vlans


def parse_vlan_members(output: str) -> dict[int, list[str]]:
    """`show vlan members` -> {vlan_id: [configured member ports]}.

    VOSS columns: VLAN-ID | PORT MEMBER | ACTIVE MEMBER | STATIC MEMBER |
    NOT_ALLOW MEMBER. PORT MEMBER (all configured members) is the first
    port-list column, so we take the first slot/port token after the VLAN id;
    'NONE' (or no port token) means the VLAN has no ports. Anchored on tokens,
    not offsets, and only rows that actually carry a port-list or an explicit
    NONE are recorded - so the 'N out of M Total' footer is ignored.

    A long PORT MEMBER list wraps onto continuation lines, broken after a
    comma. The open list is therefore carried across lines while it still ends
    in one: PORT MEMBER is the leftmost port column, so its continuation is the
    first port-list token of the next line, whatever the other columns do. A
    VLAN whose members wrapped used to be truncated at the break - on a
    48-port box with wide VLANs that silently dropped ports from the sheets.
    """
    result: dict[int, list[str]] = {}
    open_vid: int | None = None   # row whose PORT MEMBER list is still open
    raw = ""
    for line in output.splitlines():
        tokens = line.split()
        if (open_vid is not None and tokens
                and "/" in tokens[0] and is_port_list(tokens[0])):
            raw += tokens[0]
            result[open_vid] = expand_port_list(raw)
            if not raw.endswith(","):
                open_vid, raw = None, ""
            continue
        open_vid, raw = None, ""
        if len(tokens) < 2 or not tokens[0].isdigit():
            continue
        vid = int(tokens[0])
        if not 1 <= vid <= 4094:
            continue
        for t in tokens[1:]:
            if "/" in t and is_port_list(t):
                result[vid] = expand_port_list(t)
                if t.endswith(","):
                    open_vid, raw = vid, t
                break
            if t.upper() == "NONE":
                result[vid] = []
                break
    return result


def parse_vlan_basic(output: str) -> dict[int, str]:
    """`show vlan basic` -> {vlan_id: name}. Used to fill in VLAN names.

    Lines: <vlan-id> <name> <type> <mstp-inst> ...

    The 'N out of M Total Num of Vlans displayed' footer has exactly the same
    shape as a data row when a release prints it without the leading 'All'
    ('39 out of 39 ...' would otherwise become VLAN 39 named 'out'), so footers
    are excluded explicitly and the MSTP instance column must be numeric.
    """
    result: dict[int, str] = {}
    for line in output.splitlines():
        if re.search(r"\bout of\b", line, re.IGNORECASE):
            continue
        m = re.match(r"^\s*(\d{1,4})\s+(\S+)\s+(\S+)\s+(\d+)\b", line)
        if m and 1 <= int(m.group(1)) <= 4094:
            result[int(m.group(1))] = m.group(2)
    return result


def parse_isid_local(output: str) -> dict[int, dict]:
    """`show i-sid` on a DvR controller/BEB.

    Returns {isid: {"cvids": set[int], "name": str}}. Rows are anchored on a
    numeric first token (the I-SID id) followed by a non-numeric TYPE column,
    so any TYPE value works (ELAN, ELAN_TR, CVLAN, future ones) - an unknown
    type can never cause endpoints to be attributed to the previous I-SID.
    C-VIDs come from 'c<vid>:<endpoint>' markers (incl. wrapped continuation
    lines) and from the VLANID column newer releases insert after TYPE.
    Footer lines ('N out of M Total Num ...') are ignored. I-SID names may be
    arbitrary words ('quarantaine', 'cvlan-x') - only structural tokens
    (CONFIG/DISCOVER/-/N/A, ports, numbers, c<vid>: markers) are excluded.
    """
    result: dict[int, dict] = {}
    current: int | None = None
    for line in output.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if re.fullmatch(r"\d{1,9}", tokens[0]) and len(tokens) >= 2 \
                and not tokens[1].isdigit():
            if "out of" in line.lower():
                current = None  # 'N out of M Total Num ...' footer
                continue
            current = int(tokens[0])
            entry = result.setdefault(current, {"cvids": set(), "name": ""})
            if len(tokens) >= 3 and tokens[2].isdigit() \
                    and 1 <= int(tokens[2]) <= 4094:
                entry["cvids"].add(int(tokens[2]))  # VLANID column
            tail = tokens[-1]
            if len(tokens) > 2 and tail.upper() not in ("CONFIG", "DISCOVER", "-", "N/A") \
                    and not re.match(r"^c\d{1,4}:", tail) \
                    and not PORT_RE.match(tail) and not tail.isdigit():
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
