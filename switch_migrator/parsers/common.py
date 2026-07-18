"""Parsing helpers shared between platforms."""

from __future__ import annotations

import re

# 1/1  1/1/1  (VSP channelized)  49  2/49 (ERS stack)
PORT_RE = re.compile(r"^\d+(?:/\d+){0,2}$")
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


def parse_lldp_neighbors_summary(output: str) -> dict[str, str]:
    """`show lldp neighbor summary` - one-line-per-neighbor table with a
    SysName column. Returns {local_port: remote_sysname}.

    Header spelling differs per release: real VOSS 8.10 prints an ALL-CAPS
    two-line header whose second line carries 'SYSNAME', older outputs use
    'SysName' - matched case-insensitively. The column is cut by character
    offset from the header (start of SYSNAME to the start of the next header
    word, e.g. SYSDESCR), because the REMOTE PORT column may contain spaces
    ('Embedded ALOM, Po~') and the SYSNAME cell is EMPTY for non-LLDP-sysname
    neighbors (servers) - token counting would misattribute those.
    """
    neighbors: dict[str, str] = {}
    start: int | None = None
    end: int | None = None
    for line in output.splitlines():
        if start is None:
            m = re.search(r"sysname", line, re.IGNORECASE)
            if m:
                start = m.start()
                nxt = re.search(r"\S", line[m.end():])
                end = m.end() + nxt.start() if nxt else None
            continue
        tokens = line.split()
        if not tokens or not PORT_RE.match(tokens[0]):
            continue
        cell = line[start:end] if end is not None else line[start:]
        name = cell.strip().split()[0] if cell.strip() else ""
        if name:
            neighbors.setdefault(tokens[0], name)
    return neighbors


def parse_lldp_neighbors(output: str) -> dict[str, str]:
    """Parse the block-style `show lldp neighbor` output used by both VOSS and
    ERS/BOSS into {local_port: remote_sysname}.

    Blocks look like:
        Port: 1/1     Index: 1  ...
                ChassisId: MAC Address b0:ad:...
                SysName: core-sw-01
    """
    neighbors: dict[str, str] = {}
    current_port: str | None = None
    for line in output.splitlines():
        m = re.search(r"^\s*Port\s*:\s*(\S+)", line)
        if m:
            token = m.group(1).rstrip(",")
            current_port = token if PORT_RE.match(token) else None
            continue
        m = re.search(r"^\s*SysName\s*:\s*(.+?)\s*$", line)
        if m and current_port:
            name = m.group(1).strip()
            if name and current_port not in neighbors:
                neighbors[current_port] = name
    return neighbors
