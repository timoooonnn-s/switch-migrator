"""Parsing helpers shared between platforms."""

from __future__ import annotations

import logging
import re

from switch_migrator.models import LldpNeighbor

MIN_VLAN_ID = 1
MAX_VLAN_ID = 4094
# a slot/port id ('1/7') and a channelized one ('1/17/1')
SLOT_PORT_PARTS = 2
SUBPORT_PARTS = 3
# ranges wider than these are not port lists but garbage, and are not expanded
MAX_RANGE_SPAN = 512
MAX_CHANNELIZED_PARENTS = 64
MAX_SUBPORTS = 16
MAC_HEX_DIGITS = 12
# the ERS port column is a bare number, ambiguous with the VLAN column next to it
VLAN_AND_PORT = 2


def is_vlan_id(value: int) -> bool:
    """Is this a usable 802.1Q VLAN id (1-4094)?"""
    return MIN_VLAN_ID <= value <= MAX_VLAN_ID

# 1/1  1/1/1  (VSP channelized)  49  2/49 (ERS stack)
PORT_RE = re.compile(r"^\d+(?:/\d+){0,2}$")


def port_sort_key(port: str) -> tuple[int, tuple[int, ...], str]:
    """Faceplate order: '1/2' before '1/10', '2' before '10', unit by unit.

    Every run of digits compares as a number, so slot/port, channelized
    slot/port/sub-port and flat ERS numbering all sort the way the ports sit on
    the hardware. Anything without digits sorts last, by its text.
    """
    numbers = tuple(int(n) for n in re.findall(r"\d+", str(port or "")))
    return (0, numbers, "") if numbers else (1, (), str(port or ""))

log = logging.getLogger(__name__)

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _clean_ip(value: str) -> str:
    """Return a usable neighbor IP, or '' for none/placeholder values
    ('-', '0.0.0.0', an all-zero IPv6)."""
    v = value.strip()
    if not v or v == "-":
        return ""
    if _IPV4_RE.match(v):
        return "" if v == "0.0.0.0" else v
    if ":" in v and re.fullmatch(r"[0-9a-fA-F:]+", v):   # IPv6
        return "" if not any(h.strip("0") for h in v.split(":")) else v
    return ""
# port lists as they appear in MLT member columns: 1/1-1/2, 1/47,1/48, 49-50
PORT_LIST_RE = re.compile(r"^\d+(?:/\d+){0,2}(?:[,\-]\d+(?:/\d+){0,2})*$")

# One comma-separated element of a port list: '1/7', '1/1-1/10', '49-50'.
# Validated per element so a list can be checked without depending on how the
# device chose to break it across lines.
_PORT_CHUNK_RE = re.compile(r"^\d+(?:/\d+){0,2}(?:-\d+(?:/\d+){0,2})?$")


def is_port_list(raw: str) -> bool:
    """Does this token look like a port list?

    Tolerant of both marks a device leaves behind when it wraps a long list
    onto a continuation line - a trailing comma ('1/1-1/10,1/12,') and a
    trailing hyphen from a break inside a range ('1/1-1/16,1/17/1-'). That is
    exactly the case where recognising the token matters most: rejecting it
    does not merely lose the wrapped remainder, it sends the caller's column
    scan on to the NEXT column and reads that one instead.
    """
    return bool(_port_chunks(raw))


def _port_chunks(raw: str) -> list[str]:
    """Split a port list into validated elements, or [] if it is not one.

    Empty elements are dropped rather than failing the whole list: a device
    that wraps a long port list leaves a trailing comma on the first line, and
    rejecting the string outright would lose every port of that VLAN or MLT,
    not just the wrapped remainder.

    A single trailing hyphen is the other half of that: the break can land
    inside a range ('1/1-1/16,1/17/1-'), leaving a range with no end. It reads
    as the range's start, which is what the line actually named.
    """
    raw = raw.strip()
    if not raw or raw.upper() == "NONE":
        return []
    if raw.endswith("-"):
        raw = raw[:-1]
    chunks = [c.strip() for c in raw.split(",")]
    chunks = [c for c in chunks if c]
    if not chunks or not all(_PORT_CHUNK_RE.match(c) for c in chunks):
        return []
    return chunks


_UPDOWN = {"up": True, "down": False, "testing": False}

# 'ist' / 'vist' as a whole word, not as three letters inside another one -
# 'dist-uplink', 'twist', 'sister' are ordinary MLT names, not IST peer links.
_IST_NAME_RE = re.compile(r"(?:^|[^a-z0-9])v?ist(?:$|[^a-z0-9])", re.IGNORECASE)


def parse_updown(token: str) -> bool | None:
    return _UPDOWN.get(token.strip().lower())


def name_says_ist(name: str) -> bool:
    """Does this MLT name identify it as the IST/vIST peer link?"""
    return bool(_IST_NAME_RE.search(name or ""))


def _span_channelized(s_parts: list[str], e_parts: list[str]) -> list[str] | None:
    """Expand a channelized range that crosses parent ports, e.g.
    '1/17/1-1/18/4' -> 1/17/1..1/17/4, 1/18/1..1/18/4.

    VOSS prints these in `show vlan members` on any box with breakout ports.
    The channelization width is not in the output, so it is taken from the
    range's own last sub-port - the notation runs to the end of the final
    parent port, which makes that the width in every layout Extreme documents.

    Guessing wide is the safe direction here and guessing narrow is not: a
    sub-port that does not exist matches no collected port and is dropped on
    the spot, whereas one left out silently loses its VLAN. For that reason
    this is only ever used for VLAN MEMBERSHIP - never for MLT members, where
    an invented member would show as a down leg and fake a degraded MLT.
    """
    if len(s_parts) != SUBPORT_PARTS or len(e_parts) != SUBPORT_PARTS \
            or s_parts[0] != e_parts[0]:
        return None
    slot = s_parts[0]
    p_lo, p_hi = int(s_parts[1]), int(e_parts[1])
    s_lo, width = int(s_parts[2]), int(e_parts[2])
    if not 0 < p_hi - p_lo <= MAX_CHANNELIZED_PARENTS or not 0 < width <= MAX_SUBPORTS \
            or s_lo > width:
        return None
    ports = []
    for parent in range(p_lo, p_hi + 1):
        first = s_lo if parent == p_lo else 1
        for sub in range(first, width + 1):
            ports.append(f"{slot}/{parent}/{sub}")
    return ports


def expand_port_list(raw: str, span_subports: bool = False) -> list[str]:
    """Expand '1/1-1/3,1/10' -> ['1/1','1/2','1/3','1/10'].

    Ranges expand within the last element (the port number). A range that
    crosses slots or parent ports cannot be enumerated from the text alone, so
    it is kept as its two endpoints rather than guessed - and logged, because
    the ports in between are silently absent from whatever asked for the list.
    `span_subports` opts into enumerating the channelized case (see
    `_span_channelized`); callers whose result must not gain a phantom port
    leave it off.

    A trailing comma (a list the device wrapped onto a continuation line) is
    tolerated and expands to the ports named so far.
    """
    ports: list[str] = []
    for chunk in _port_chunks(raw):
        if "-" not in chunk:
            ports.append(chunk)
            continue
        start, end = chunk.split("-", 1)
        s_parts, e_parts = start.split("/"), end.split("/")
        if len(s_parts) == len(e_parts) and s_parts[:-1] == e_parts[:-1]:
            prefix = "/".join(s_parts[:-1])
            lo, hi = int(s_parts[-1]), int(e_parts[-1])
            if 0 <= hi - lo <= MAX_RANGE_SPAN:
                for n in range(lo, hi + 1):
                    ports.append(f"{prefix}/{n}" if prefix else str(n))
                continue
        if span_subports:
            spanned = _span_channelized(s_parts, e_parts)
            if spanned is not None:
                ports.extend(spanned)
                continue
        log.info("port range '%s' crosses slots/ports and cannot be enumerated "
                 "from the output - keeping its endpoints only; the ports "
                 "between them will be missing", chunk)
        ports.extend([start, end])
    return ports


MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}[:.-]){5}[0-9a-fA-F]{2}$"
                    r"|^(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}$")


def normalize_mac(raw: str) -> str:
    """'00:11:22:33:44:55' / '0011.2233.4455' / '00-11-22-33-44-55' -> lower
    colon form, so MACs from different platforms compare and sort alike."""
    hexs = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if len(hexs) != MAC_HEX_DIGITS:
        return raw.strip().lower()
    return ":".join(hexs[i:i + 2] for i in range(0, MAC_HEX_DIGITS, 2))


# tokens that are status/flag words, never an interface
_FDB_KEYWORDS = {
    "learned", "self", "dynamic", "static", "mgmt", "invalid", "config",
    "true", "false", "-", "cpu", "secure", "aging", "other", "management",
}
# 'Port-1/7', 'Port:48', 'Port 48' (VOSS / ERS spell it differently)
_IFACE_PORT_RE = re.compile(r"^Port[-: ]?(\d+(?:/\d+){0,2})$", re.IGNORECASE)
# 'Mlt-35', 'MLT:35', 'Trunk:1'
_IFACE_MLT_RE = re.compile(r"^(?:Mlt|Trunk)[-: ]?(\d+)$", re.IGNORECASE)


def parse_mac_table(output: str) -> dict[str, list[tuple[str, int | None]]]:
    """Forwarding-database output -> {interface: [(mac, vlan)]}.

    Covers VOSS `show interfaces gigabitEthernet fdb-entry` and ERS/BOSS
    `show mac-address-table`, whose column ORDER and interface SPELLING both
    differ, so rows are matched by shape: a line is an entry when it holds
    exactly one MAC-shaped token. The interface is then resolved in order of
    decreasing certainty:

      1. an explicitly labelled column - 'Port-1/7', 'Port:48', 'Mlt-35',
         'Trunk:1', 'mlt 35' (unambiguous, so it wins);
      2. a bare slot/port token ('1/7');
      3. on ERS the port column can be a bare number, which is ambiguous with
         the VLAN column - the LAST bare number is the port, the first is the
         VLAN;
      4. anything else non-numeric that is not a status word is an MLT NAME
         (real VOSS prints the MLT's name, not 'Mlt-<id>') and is returned as
         'name:<name>' for the caller to resolve against the known MLTs.

    MLT entries are returned under 'mlt:<id>' so the caller can fan them out to
    the member ports.
    """
    result: dict[str, list[tuple[str, int | None]]] = {}
    for line in output.splitlines():
        tokens = line.split()
        macs = [t for t in tokens if MAC_RE.match(t)]
        if len(macs) != 1:
            continue
        mac = normalize_mac(macs[0])
        rest = [t for t in tokens if not MAC_RE.match(t)]

        numbers = [t for t in rest if t.isdigit()]
        iface = _fdb_interface(rest, numbers)
        if iface is None:
            continue
        # the VLAN is the first small integer that is not the interface itself
        vlan = next((int(t) for t in numbers
                     if t != iface and is_vlan_id(int(t))), None)
        result.setdefault(iface, []).append((mac, vlan))
    return result


def _labelled_interface(rest: list[str]) -> str | None:
    """(1) An explicitly labelled column: 'Port-1/7', 'Mlt-35', 'mlt 35'."""
    for t in rest:
        m = _IFACE_PORT_RE.match(t)
        if m:
            return m.group(1)
        m = _IFACE_MLT_RE.match(t)
        if m:
            return f"mlt:{m.group(1)}"
    for i, t in enumerate(rest[:-1]):                    # 'mlt 35' / 'Trunk 1'
        if t.lower() in ("mlt", "trunk") and rest[i + 1].isdigit():
            return f"mlt:{rest[i + 1]}"
    return None


def _fdb_interface(rest: list[str], numbers: list[str]) -> str | None:
    """The interface of one FDB row, in order of decreasing certainty (see
    parse_mac_table)."""
    iface = _labelled_interface(rest)
    if iface is not None:
        return iface
    slots = [t for t in rest if PORT_RE.match(t) and "/" in t]   # (2) bare slot/port
    if slots:
        return slots[-1]
    if len(numbers) >= VLAN_AND_PORT:                    # (3) ERS bare numbers
        return numbers[-1]
    names = [t for t in rest                             # (4) an MLT name
             if not t.isdigit() and t.lower() not in _FDB_KEYWORDS
             and not PORT_RE.match(t)]
    return f"name:{names[0]}" if names else None


def parse_lldp_neighbors_summary(output: str) -> dict[str, LldpNeighbor]:
    """`show lldp neighbor summary` -> {local_port: LldpNeighbor}.

    A fixed-width table. Columns are cut by character offset taken from the
    header line that carries SYSNAME (ALL-CAPS on VOSS 8.10, mixed-case on
    older releases - matched case-insensitively): each column runs from its
    header word to the next, so the SYSNAME cell is EMPTY for neighbors that
    advertise no name (HP iLO) and the wider REMOTE PORT / SYSDESCR columns
    can't bleed into it. SYSNAME here is truncated by the device on long names
    (e.g. an adapter model shows as '10/25Gb 2-po~'); the IP is taken from the
    IP/IPv6 ADDR column so a neighbor is still identifiable.
    """
    neighbors: dict[str, LldpNeighbor] = {}
    cols: dict[str, tuple[int, int | None]] | None = None
    for line in output.splitlines():
        if cols is None:
            if re.search(r"sysname", line, re.IGNORECASE):
                cols = _column_bounds(line, ("ADDR", "SYSNAME", "SYSDESCR"))
            continue
        tokens = line.split()
        if not tokens or not PORT_RE.match(tokens[0]):
            continue
        name = _cell(line, cols["SYSNAME"])
        neighbors.setdefault(tokens[0], LldpNeighbor(
            sysname=name.split()[0] if name else "",
            ip=_clean_ip(_cell(line, cols["ADDR"])),
            sys_descr=_cell(line, cols["SYSDESCR"])))
    return neighbors


def _column_bounds(header: str, names: tuple[str, ...]
                   ) -> dict[str, tuple[int, int | None]]:
    """{column: (start, end)} from a fixed-width header line: a column runs from
    its header word to the next header word. (-1, None) for a missing column."""
    words = [(m.group(), m.start()) for m in re.finditer(r"\S+", header)]
    starts = sorted(s for _, s in words)
    bounds: dict[str, tuple[int, int | None]] = {}
    for name in names:
        start = next((s for w, s in words if w.upper() == name), -1)
        after = [x for x in starts if x > start] if start >= 0 else []
        bounds[name] = (start, min(after) if after else None)
    return bounds


def _cell(line: str, bounds: tuple[int, int | None]) -> str:
    start, end = bounds
    if start < 0:
        return ""
    return (line[start:end] if end is not None else line[start:]).strip()


def parse_lldp_neighbors(output: str) -> dict[str, LldpNeighbor]:
    """Parse the block-style `show lldp neighbor` (VOSS and ERS/BOSS) into
    {local_port: LldpNeighbor}.

    Blocks look like:
        Port: 1/1     Index: 1  ...
                ChassisId: MAC Address b0:ad:...
                SysName  : core-sw-01
                SysDescr : VSP-7254XSQ (8.10.9.0)
                Address  : 10.0.0.1
    One local port can carry several neighbor blocks (e.g. an empty one plus a
    real one); the block that actually advertises a SysName wins, else one with
    an IP, else the first - fields are never mixed across neighbors.
    """
    by_port: dict[str, list[dict[str, str]]] = {}
    cur: dict[str, str] | None = None
    for line in output.splitlines():
        m = re.match(r"^\s*Port\s*:\s*(\S+)", line)
        if m:
            token = m.group(1).rstrip(",")
            if PORT_RE.match(token):
                cur = {"sysname": "", "ip": "", "sys_descr": ""}
                by_port.setdefault(token, []).append(cur)
            else:
                cur = None
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*SysName\s*:\s*(.*)$", line)
        if m:
            cur["sysname"] = m.group(1).strip()
            continue
        m = re.match(r"^\s*SysDescr\s*:\s*(.*)$", line)
        if m:
            cur["sys_descr"] = m.group(1).strip()
            continue
        m = re.match(r"^\s*(?:IPv6\s+)?Address\s*:\s*(.*)$", line)
        if m and not cur["ip"]:
            cur["ip"] = _clean_ip(m.group(1))

    neighbors: dict[str, LldpNeighbor] = {}
    for port, blocks in by_port.items():
        best = (next((b for b in blocks if b["sysname"]), None)
                or next((b for b in blocks if b["ip"]), None)
                or blocks[0])
        neighbors[port] = LldpNeighbor(best["sysname"], best["ip"], best["sys_descr"])
    return neighbors
