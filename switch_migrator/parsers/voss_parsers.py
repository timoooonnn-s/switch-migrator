"""Parsers for VOSS / Fabric Engine CLI output (tested against 8.x-9.4.x
formats). All parsers are deliberately tolerant: they anchor on stable tokens
(port IDs, integer IDs, up/down keywords) instead of column positions, because
column layout shifts between VOSS releases.
"""

from __future__ import annotations

import re
from typing import TypedDict

from switch_migrator.models import IstState, MltState, PortIsidRow, PortState, VlanInfo
from switch_migrator.parsers.common import (
    PORT_RE,
    TEXT,
    TOKEN,
    expand_port_list,
    is_port_list,
    is_vlan_id,
    name_says_ist,
    parse_updown,
    spilled_cells,
)

_RULE_RE = re.compile(r"\s*-{10,}\s*")


def _header_above_rule(lines: list[str]) -> str:
    """The last header line of a VOSS table: the line above its dashed rule.

    Column LABELS are the only thing in this output that says which release
    printed it, and 9.x inserts columns (VLAN NAME in `show vlan i-sid`, ISID
    NAME / AREA in the SPBM tables) that shift everything after them. Reading
    the label row is what keeps a 9.x column from being mistaken for an 8.x
    one.
    """
    for i, line in enumerate(lines):
        if _RULE_RE.fullmatch(line) and i:
            return lines[i - 1]
    return ""


def _phrase_columns(header: str, *phrases: str) -> list[tuple[int, int | None]] | None:
    """(start, end) of each column of a header whose labels contain spaces.

    Phrases are found left to right, each after the previous one, so 'I-SID
    NAME' is one column and not two - which a header split into single
    words, cannot express. None when any phrase is missing, so the caller can
    try a different column set (the 8.x one) or fall back.
    """
    starts: list[int] = []
    at = 0
    for phrase in phrases:
        pattern = (r"(?<!\S)" + r"\s+".join(re.escape(w) for w in phrase.split())
                   + r"(?!\S)")
        found = re.compile(pattern, re.IGNORECASE).search(header, at)
        if found is None:
            return None
        starts.append(found.start())
        at = found.end()
    return [(start, starts[i + 1] if i + 1 < len(starts) else None)
            for i, start in enumerate(starts)]

# fields a data row of each table has at least, and the columns read by index
MEDIA_COLUMN = 2            # Port Interface: PORT NUM | INDEX | DESCRIPTION
ADMIN_AND_OPER = 2          # the two up/down tokens of a port row
MIN_MLT_FIELDS = 3
MIN_PORT_STATE_FIELDS = 3
STATE_REASON_COLUMN = 3
MIN_PORT_ISID_FIELDS = 5
VIST_FIELDS = 4             # peer-ip | vlan | enabled | session
MIN_VLAN_MEMBERS_FIELDS = 2
MIN_ISID_ROW_FIELDS = 2     # I-SID id | type
ISID_VLAN_COLUMN = 2        # ... | VLANID
MIN_DVR_FIELDS = 5
MIN_SPBM_ISID_FIELDS = 4
# `show vlan i-sid`: VLAN_ID | I-SID | VLAN NAME | I-SID NAME on 9.x, and the
# same without VLAN NAME on 8.x
VLAN_ISID_COLUMNS_9X = 4
# `show isis spbm i-sid all`: HOST_NAME is the sixth column on every release
SPBM_HOST_COLUMN = 5
# 9.x `show i-sid` ORIGIN: one flag slot per source, 'C  ---  -  ---  -  -  --'
_ORIGIN_FLAGS = re.compile(r"(?:[A-Za-z]{1,2}|-{1,3})(?:  (?:[A-Za-z]{1,2}|-{1,3}))*")

_UPDOWN_TOKEN = re.compile(r"^(up|down|testing)$", re.IGNORECASE)


_MAC_TOKEN = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")



class LocalIsid(TypedDict):
    """One I-SID of `show i-sid`: the c-vids attached here, and its name."""
    cvids: set[int]
    name: str


class DvrInterfaceRow(TypedDict):
    """One L2 I-SID <-> VLAN pair of `show dvr interfaces`."""
    l2isid: int
    vlan: int


class SpbmIsidRow(TypedDict):
    """One row of `show isis spbm i-sid all`."""
    isid: int
    type: str
    host: str

def _media(tokens: list[str]) -> str:
    """DESCRIPTION column of the Port Interface table ('10GbSR', '1000BaseTX').

    It is the third column, but it is blank on ports with no media, which
    shifts every following column left - so the token is only taken when it
    actually looks like a description and not like the LINK TRAP / MTU / MAC
    columns that slide into its place.
    """
    if len(tokens) <= MEDIA_COLUMN:
        return ""
    tok = tokens[MEDIA_COLUMN]
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
        if len(updown) < ADMIN_AND_OPER:
            continue
        seen.add(tokens[0])
        ports.append(PortState(
            port=tokens[0],
            media=_media(tokens),
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
                and all(t.isdigit() and is_vlan_id(int(t)) for t in tokens)):
            current.vlans.extend(int(t) for t in tokens
                                 if int(t) not in current.vlans)
            continue
        current = None
        if len(tokens) < MIN_MLT_FIELDS or not tokens[0].isdigit():
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
        vlans = [int(t) for t in _vlan_ids_tail(rest, members_at)
                 if t.isdigit() and is_vlan_id(int(t))]
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


def _vlan_ids_tail(rest: list[str], members_at: int | None) -> list[str]:
    """The tokens of the VLAN IDS column of one Mlt Info row.

    Normally everything after the PORT MEMBERS column. An MLT whose members
    are all gone - or an LACP MLT whose ports are configured but not yet up -
    prints an EMPTY members column, and keying off it dropped the VLAN IDS
    too: the MLT reached the sheet carrying no VLANs, and `--generate-mlt`
    would have recreated it empty. With no members column the list is still
    the trailing run of VLAN-sized numbers; ORIGIN ('CONFIG') sits in front of
    it and is a word, which is what ends the run.
    """
    if members_at is not None:
        return rest[members_at + 1:]
    tail: list[str] = []
    for token in reversed(rest):
        if not (token.isdigit() and is_vlan_id(int(token))):
            break
        tail.insert(0, token)
    return tail


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
        if len(tokens) < MIN_PORT_STATE_FIELDS or not PORT_RE.match(tokens[0]):
            continue
        admin, oper = parse_updown(tokens[1]), parse_updown(tokens[2])
        if admin is None or oper is None:
            continue
        reason = (tokens[STATE_REASON_COLUMN] if len(tokens) > STATE_REASON_COLUMN
                  and tokens[STATE_REASON_COLUMN] != "--" else "")
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


def parse_port_isid(output: str) -> list[PortIsidRow]:
    """`show interfaces gigabitEthernet i-sid`

    Data lines: <port> <ifindex> <isid> <vlanid|N/A> <c-vid|N/A> <type> ...
    Returns [{"port": str, "isid": int, "vlan": int|None}, ...]; the VLAN is
    taken from VLANID, falling back to C-VID for switched-UNI rows.
    """
    rows: list[PortIsidRow] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < MIN_PORT_ISID_FIELDS or not PORT_RE.match(tokens[0]) \
                or not tokens[1].isdigit() or not tokens[2].isdigit():
            continue
        vlan = None
        for candidate in (tokens[3], tokens[4]):
            if candidate.isdigit() and is_vlan_id(int(candidate)):
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
        if len(tokens) >= VIST_FIELDS and re.match(r"^\d+\.\d+\.\d+\.\d+$", tokens[0]):
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

    8.x columns: VLAN_ID | I-SID | I-SID NAME
    9.x columns: VLAN_ID | I-SID | VLAN NAME | I-SID NAME

    9.x inserts the VLAN's OWN name between the two. Read as 8.x, the row
    `40  20040  servers  ISID-20040` made the I-SID name
    'servers                          ISID-20040' - two different names glued
    into one cell, which then travelled into the VLAN name column of every
    sheet and into the fabric state's I-SID names. So the header decides how
    many columns there are, and the VLAN name is kept where it belongs - which
    also means the name no longer depends on `show vlan basic` answering.
    """
    lines = output.splitlines()
    columns = _vlan_isid_columns(_header_above_rule(lines))
    vlans: list[VlanInfo] = []
    for line in lines:
        if re.search(r"\bout of\b", line, re.IGNORECASE):
            continue    # 'N out of M Total Num of Vlans displayed'
        info = (_vlan_isid_by_column(line, columns) if columns
                else _vlan_isid_by_shape(line))
        if info is not None:
            vlans.append(info)
    return vlans


def _vlan_isid_columns(header: str) -> list[tuple[int, re.Pattern[str] | None]] | None:
    """The 9.x four-column header, else the 8.x three-column one, else None.

    VLAN NAME is 33 characters wide but a VLAN name may be 64, so it is read
    as TEXT that can run into I-SID NAME rather than cut at the header.
    """
    if "VLAN_ID" not in header.upper():
        return None
    nine = _phrase_columns(header, "VLAN_ID", "I-SID", "VLAN NAME", "I-SID NAME")
    if nine:
        return _shaped(nine, TOKEN, TOKEN, TEXT, TEXT)
    eight = _phrase_columns(header, "VLAN_ID", "I-SID", "I-SID NAME")
    return _shaped(eight, TOKEN, TOKEN, TEXT) if eight else None


def _shaped(columns: list[tuple[int, int | None]],
            *shapes: re.Pattern[str] | None) -> list[tuple[int, re.Pattern[str] | None]]:
    """`_phrase_columns` output in the form `spilled_cells` reads."""
    return [(start, shape) for (start, _), shape in zip(columns, shapes, strict=True)]


def _vlan_isid_by_column(line: str,
                         columns: list[tuple[int, re.Pattern[str] | None]]
                         ) -> VlanInfo | None:
    cells = spilled_cells(line, columns)
    if not cells[0].isdigit() or not is_vlan_id(int(cells[0])):
        return None
    if cells[1] and not cells[1].isdigit():
        return None            # a footer or a legend line, not a VLAN row
    vlan_name, isid_name = ((cells[2], cells[3]) if len(cells) == VLAN_ISID_COLUMNS_9X
                            else ("", cells[2]))
    return VlanInfo(vlan_id=int(cells[0]),
                    name=vlan_name,
                    isid=int(cells[1]) if cells[1] else None,
                    isid_name=isid_name)


def _vlan_isid_by_shape(line: str) -> VlanInfo | None:
    """No header in this output (a hand-made capture): read it as 8.x.

    Anchored with \\s+ separators so numeric footer lines ('3 out of 3 Total
    Num of Vlans displayed') can never match.
    """
    m = re.match(r"^\s*(\d{1,4})(?:\s+(\d{1,9})(?:\s+(\S.*?))?)?\s*$", line)
    if not m or not is_vlan_id(int(m.group(1))):
        return None
    return VlanInfo(vlan_id=int(m.group(1)),
                    isid=int(m.group(2)) if m.group(2) else None,
                    isid_name=(m.group(3) or "").strip())


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
    first port-list token of the next line, whatever the other columns do.
    Stopping at the break would silently drop ports from the sheets on any
    48-port box with wide VLANs.
    """
    result: dict[int, list[str]] = {}
    open_vid: int | None = None   # row whose PORT MEMBER list is still open
    raw = ""
    for line in output.splitlines():
        tokens = line.split()
        if (open_vid is not None and tokens
                and "/" in tokens[0] and is_port_list(tokens[0])):
            raw += tokens[0]
            result[open_vid] = expand_port_list(raw, span_subports=True)
            if not raw.endswith((",", "-")):
                open_vid, raw = None, ""
            continue
        open_vid, raw = None, ""
        if len(tokens) < MIN_VLAN_MEMBERS_FIELDS or not tokens[0].isdigit():
            continue
        vid = int(tokens[0])
        if not is_vlan_id(vid):
            continue
        for t in tokens[1:]:
            if "/" in t and is_port_list(t):
                # VLAN membership, so a channelized range is enumerated: an
                # invented sub-port matches no collected port and vanishes,
                # while a missing one costs that port its VLAN
                result[vid] = expand_port_list(t, span_subports=True)
                if t.endswith((",", "-")):
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
        if m and is_vlan_id(int(m.group(1))):
            result[int(m.group(1))] = m.group(2)
    return result


def parse_isid_local(output: str) -> dict[int, LocalIsid]:
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
    lines = output.splitlines()
    name_column = _isid_columns(lines)
    result: dict[int, LocalIsid] = {}
    current: int | None = None
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        if re.fullmatch(r"\d{1,9}", tokens[0]) and len(tokens) >= MIN_ISID_ROW_FIELDS \
                and not tokens[1].isdigit():
            if "out of" in line.lower():
                current = None  # 'N out of M Total Num ...' footer
                continue
            current = int(tokens[0])
            entry = result.setdefault(current, {"cvids": set(), "name": ""})
            if len(tokens) > ISID_VLAN_COLUMN and tokens[ISID_VLAN_COLUMN].isdigit() \
                    and is_vlan_id(int(tokens[ISID_VLAN_COLUMN])):
                entry["cvids"].add(int(tokens[ISID_VLAN_COLUMN]))
            name = _isid_name(line, tokens, name_column)
            if name:
                entry["name"] = name
        if current is None:
            continue
        for m in re.finditer(r"\bc(\d{1,4}):", line):
            result[current]["cvids"].add(int(m.group(1)))
    return result


def _isid_columns(lines: list[str]) -> list[tuple[int, re.Pattern[str] | None]] | None:
    """Every column of the `show i-sid` header, NAME last; None without one.

    The NAME column is read by position: it is the only cell of this table
    that may contain spaces, and taking the last TOKEN instead turned
    'Onboarding I-SID' into 'I-SID' on every release. But a long port list
    runs past its column and pushes ORIGIN along with it, so cutting at the
    NAME offset alone read 'CONFIG     Server-100' as the name: every column
    before it is followed (`spilled_cells`). 8.x labels them all on the last
    header line; 9.x splits the header over two lines and prints ORIGIN only
    on the first, as a run of flag slots.
    """
    header = _header_above_rule(lines)
    eight = _phrase_columns(header, "ID", "TYPE", "INTERFACES", "INTERFACES",
                            "ORIGIN", "ISID NAME")
    if eight:
        return _shaped(eight, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TEXT)
    nine = _phrase_columns(header, "ID", "TYPE", "VLANID", "INTERFACES",
                           "INTERFACES", "NAME")
    above = lines[lines.index(header) - 1] if header and lines.index(header) else ""
    origin = above.upper().find("ORIGIN")
    if nine and nine[4][0] < origin < nine[5][0]:
        return [*_shaped(nine[:5], TOKEN, TOKEN, TOKEN, TOKEN, TOKEN),
                (origin, _ORIGIN_FLAGS), (nine[5][0], TEXT)]
    # some other layout: still read NAME where its label is
    name = _phrase_columns(header, "ISID NAME") or _phrase_columns(header, "NAME")
    return _shaped(name, TEXT) if name else None


def _isid_name(line: str, tokens: list[str],
               name_column: list[tuple[int, re.Pattern[str] | None]] | None) -> str:
    """The I-SID NAME cell of one `show i-sid` row.

    From the header's columns when the output has them; otherwise the old
    last-token rule, which is all a header-less capture allows.
    """
    if name_column is not None:
        return spilled_cells(line, name_column)[-1]
    tail = tokens[-1]
    if len(tokens) > ISID_VLAN_COLUMN \
            and tail.upper() not in ("CONFIG", "DISCOVER", "-", "N/A") \
            and not re.match(r"^c\d{1,4}:", tail) \
            and not PORT_RE.match(tail) and not tail.isdigit():
        return tail
    return ""


def parse_dvr_interfaces(output: str) -> list[DvrInterfaceRow]:
    """`show dvr interfaces` on a DvR controller.

    Data lines: <ip> <mask> <l3isid> <vrfid> <l2isid> <vlan> <gw-ipv4> ...
    Column spacing varies, so rows are matched by shape: an IPv4 first token,
    then somewhere the triple (integer, VLAN-sized integer, IPv4) which is
    (L2ISID, VLAN, GW). Returns [{"l2isid": int, "vlan": int}, ...].
    """
    ipv4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
    rows: list[DvrInterfaceRow] = []
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < MIN_DVR_FIELDS or not ipv4.match(tokens[0]):
            continue
        for i in range(1, len(tokens) - 2):
            if tokens[i].isdigit() and tokens[i + 1].isdigit() \
                    and ipv4.match(tokens[i + 2]):
                l2isid, vlan = int(tokens[i]), int(tokens[i + 1])
                if l2isid > 0 and is_vlan_id(vlan):
                    rows.append({"l2isid": l2isid, "vlan": vlan})
                    break
    return rows


def _spbm_isid_columns(header: str) -> list[tuple[int, re.Pattern[str] | None]] | None:
    """The 9.x header, else the 8.x one. HOST_NAME is a TOKEN that may run
    past its 31 characters into ISID NAME on 9.x - cut at the header, a long
    sysname named a BEB that does not exist."""
    nine = _phrase_columns(header, "ISID", "SOURCE NAME", "VLAN", "SYSID", "TYPE",
                           "HOST_NAME", "ISID NAME", "AREA", "AREA NAME")
    if nine:
        return _shaped(nine, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TEXT,
                       TOKEN, TEXT)
    eight = _phrase_columns(header, "ISID", "SOURCE NAME", "VLAN", "SYSID", "TYPE",
                            "HOST_NAME")
    return _shaped(eight, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TEXT) if eight else None


def parse_isis_spbm_isid(output: str) -> list[SpbmIsidRow]:
    """`show isis spbm i-sid all`

    8.x columns: ISID | SOURCE NAME | VLAN | SYSID | TYPE | HOST_NAME
    9.x appends:  ISID NAME | AREA | AREA NAME

    Returns [{"isid": int, "type": str, "host": str}, ...]. The VLAN column is
    the B-VLAN, NOT a customer VLAN - intentionally ignored.

    HOST_NAME is read by position. On 9.x it is no longer the last column, and
    taking the last token made the host of every announced I-SID 'HOME' (the
    AREA column) - so the fabric report named one area instead of the BEBs
    that actually advertise each service.
    """
    lines = output.splitlines()
    columns = _spbm_isid_columns(_header_above_rule(lines))
    rows: list[SpbmIsidRow] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) < MIN_SPBM_ISID_FIELDS or not re.match(r"^\d{1,9}$", tokens[0]):
            continue
        typ = next((t for t in tokens if t.lower() in ("config", "discover")), "")
        if not typ:
            continue
        if columns:
            host = spilled_cells(line, columns)[SPBM_HOST_COLUMN]
        else:   # no header in this output: all a token rule can do
            host = tokens[-1] if tokens[-1].lower() not in ("config", "discover") else ""
        rows.append({"isid": int(tokens[0]), "type": typ.lower(), "host": host})
    return rows
