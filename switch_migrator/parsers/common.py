"""Parsing helpers shared between platforms."""

from __future__ import annotations

import re

from switch_migrator.models import LldpNeighbor

# 1/1  1/1/1  (VSP channelized)  49  2/49 (ERS stack)
PORT_RE = re.compile(r"^\d+(?:/\d+){0,2}$")

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

_UPDOWN = {"up": True, "down": False, "testing": False}


def parse_updown(token: str) -> bool | None:
    return _UPDOWN.get(token.strip().lower())


def expand_port_list(raw: str) -> list[str]:
    """Expand '1/1-1/3,1/10' -> ['1/1','1/2','1/3','1/10'].

    Ranges only expand within the last element (the port number); ranges that
    cross slots/units are kept as their two endpoints rather than guessed.
    """
    ports: list[str] = []
    raw = raw.strip()
    if not raw or raw.upper() == "NONE" or not PORT_LIST_RE.match(raw):
        return ports
    for chunk in raw.split(","):
        if "-" not in chunk:
            ports.append(chunk)
            continue
        start, end = chunk.split("-", 1)
        s_parts, e_parts = start.split("/"), end.split("/")
        if len(s_parts) == len(e_parts) and s_parts[:-1] == e_parts[:-1]:
            prefix = "/".join(s_parts[:-1])
            lo, hi = int(s_parts[-1]), int(e_parts[-1])
            if 0 <= hi - lo <= 512:
                for n in range(lo, hi + 1):
                    ports.append(f"{prefix}/{n}" if prefix else str(n))
                continue
        ports.extend([start, end])
    return ports


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
                words = [(mm.group(), mm.start()) for mm in re.finditer(r"\S+", line)]
                starts = sorted(s for _, s in words)

                def bound(name: str) -> tuple[int, int | None]:
                    for w, s in words:
                        if w.upper() == name:
                            after = [x for x in starts if x > s]
                            return s, (min(after) if after else None)
                    return -1, None

                cols = {c: bound(c) for c in ("ADDR", "SYSNAME", "SYSDESCR")}
            continue
        tokens = line.split()
        if not tokens or not PORT_RE.match(tokens[0]):
            continue

        def cell(name: str) -> str:
            s, e = cols[name]
            if s < 0:
                return ""
            return (line[s:e] if e is not None else line[s:]).strip()

        sysname = cell("SYSNAME").split()[0] if cell("SYSNAME") else ""
        neighbors.setdefault(tokens[0], LldpNeighbor(
            sysname=sysname, ip=_clean_ip(cell("ADDR")), sys_descr=cell("SYSDESCR")))
    return neighbors


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
    by_port: dict[str, list[dict]] = {}
    cur: dict | None = None
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
