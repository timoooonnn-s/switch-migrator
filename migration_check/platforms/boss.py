"""BOSS (ERS lineage): raw output -> DeviceState (decision 0006).

Shapes are taken from switch-migrator's work fixtures (`tests/fixtures/ers/`). The MAC
table has the least evidence: one three-row sample. Lines that look like data but do
not fit are reported as problems, never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from migration_check.model import DeviceState, MacSighting, Neighbor, Platform, PortStatus
from migration_check.platforms.common import enabled_capabilities, normalize_mac
from migration_check.snapshot import RawDevice

MAC_TABLE = "show mac-address-table"
LLDP = "show lldp neighbor"
MLTS = "show mlt"
VLANS = "show vlan"
PORTS = "show interfaces"

COMMANDS = (MAC_TABLE, LLDP, MLTS, VLANS, PORTS)
REQUIRED = frozenset(COMMANDS)
NETMIKO_DEVICE_TYPE = "extreme_ers"

_ERROR_PREFIXES = ("% ", "%invalid", "% invalid")

_PORT = r"\d+(?:/\d+)?"  # '7' standalone, '2/7' in a stack
_PORT_RE = re.compile(rf"^{_PORT}$")
_MAC_TOKEN = r"(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}|(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}"
_MAC_TOKEN_RE = re.compile(_MAC_TOKEN)
# '0011.2200.0011       735 Dynamic Port:7'. finditer, so two entries on one line
# (same columns) are read as two.
_MAC_ENTRY_RE = re.compile(
    rf"(?P<mac>{_MAC_TOKEN})\s+(?P<vlan>\d{{1,4}})\s+(?P<type>[A-Za-z][\w-]*)\s+(?P<source>\S+)"
)
_MAC_COUNT_RE = re.compile(r"Number of addresses:\s*(\d+)", re.IGNORECASE)
_OWN_TYPES = frozenset({"self", "mgmt", "management", "cpu"})

_LLDP_PORT_RE = re.compile(r"^\s*Port:\s*(\S+)")
_LLDP_FIELD_RE = re.compile(r"^\s*(ChassisId|PortId|SysName|SysCap|Address)\s*:\s*(.*?)\s*$")
_VLAN_ROW_RE = re.compile(r"^\s*(\d{1,4})\s+(.+?)\s+(\S+)\s+(\S+)\s+0x[0-9a-fA-F]{4}\b")


class _Problems(list[str]):
    def not_read(self, command: str, line: str, reason: str) -> None:
        self.append(f"{command!r}: line not read: {line.strip()!r} ({reason})")


def read_device(raw: RawDevice) -> DeviceState:
    problems = _Problems()

    def text_of(command: str) -> str:
        text = raw.output(command)
        if text is None:
            problems.append(raw.why_missing(command))
            return ""
        if text.lstrip().lower().startswith(_ERROR_PREFIXES):
            problems.append(f"{command!r}: switch reported: {text.strip().splitlines()[0]}")
            return ""
        return text

    mlts = parse_mlts(text_of(MLTS), problems)
    return DeviceState(
        name=raw.name,
        platform=Platform.BOSS,
        macs=tuple(parse_mac_table(raw.name, text_of(MAC_TABLE), mlts, problems)),
        neighbors=tuple(parse_lldp(raw.name, text_of(LLDP), problems)),
        ports=tuple(parse_ports(raw.name, text_of(PORTS))),
        vlans=parse_vlans(text_of(VLANS)),
        problems=tuple(problems),
    )


def expand_ports(printed: str, problems: list[str] | None = None) -> tuple[str, ...]:
    """'1-3,7' -> ('1','2','3','7'); '1/1-1/3' and '1/1-3' -> ('1/1','1/2','1/3').
    'NONE' -> (). A range across stack units cannot be expanded without knowing each
    unit's port count, so it is reported."""
    if printed.strip().upper() in {"", "NONE"}:
        return ()
    ports: list[str] = []
    for chunk in filter(None, (part.strip() for part in printed.split(","))):
        first, _, last = chunk.partition("-")
        if not last:
            ports.append(first)
            continue
        unit, _, start = first.rpartition("/")
        last_unit, _, end = last.rpartition("/")
        if last_unit and last_unit != unit:
            if problems is not None:
                problems.append(f"port range {chunk!r} spans stack units; not expanded")
            ports.extend([first, last])
            continue
        prefix = f"{unit}/" if unit else ""
        ports.extend(f"{prefix}{number}" for number in range(int(start), int(end) + 1))
    return tuple(ports)


def parse_mlts(text: str, problems: list[str]) -> dict[int, tuple[str, ...]]:
    """`show mlt` -> {MLT ID: member ports}, enabled MLTs with members only.

    The name can hold spaces ('UPLINK MLT'), so a row is anchored on its STATUS word
    from the right; MEMBERS is three columns before it (after BPDU and MODE).
    """
    members_by_id: dict[int, tuple[str, ...]] = {}
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 6 or not tokens[0].isdigit():
            continue
        status_at = next(
            (
                i
                for i in range(len(tokens) - 1, 0, -1)
                if tokens[i].lower() in ("enabled", "disabled")
            ),
            None,
        )
        if status_at is None or status_at < 4:
            continue
        if tokens[status_at].lower() == "enabled":
            members = expand_ports(tokens[status_at - 3], problems)
            if members:
                members_by_id[int(tokens[0])] = members
    return members_by_id


def parse_mac_table(
    device: str, text: str, mlts: dict[int, tuple[str, ...]], problems: _Problems
) -> Iterator[MacSighting]:
    count = 0
    expected: int | None = None
    for line in text.splitlines():
        if total := _MAC_COUNT_RE.search(line):
            expected = int(total.group(1))
            continue
        entries = list(_MAC_ENTRY_RE.finditer(line))
        if not entries:
            if _MAC_TOKEN_RE.search(line):
                problems.not_read(MAC_TABLE, line, "a MAC without VID, type and source")
            continue
        for entry in entries:
            sighting = _mac_sighting(device, entry, mlts)
            if sighting is None:
                problems.not_read(MAC_TABLE, line, f"unknown source {entry['source']!r}")
                continue
            count += 1
            yield sighting
    if expected is not None and expected != count:
        problems.append(
            f"{MAC_TABLE!r}: the switch reports {expected} addresses, {count} were read"
        )


def _mac_sighting(
    device: str, entry: re.Match[str], mlts: dict[int, tuple[str, ...]]
) -> MacSighting | None:
    mac = normalize_mac(entry["mac"])
    kind, _, where = entry["source"].partition(":")
    if mac is None or not where:
        return None
    mlt_id: int | None = None
    if kind.lower() == "port" and _PORT_RE.match(where):
        ports: tuple[str, ...] = (where,)
    elif kind.lower() in ("trunk", "mlt") and where.isdigit():
        mlt_id = int(where)
        ports = mlts.get(mlt_id, ())  # a disabled or unknown MLT stays unresolved
    else:
        return None
    return MacSighting(
        device=device,
        mac=mac,
        vlan_id=int(entry["vlan"]),
        interface=entry["source"],
        ports=ports,
        mlt_id=mlt_id,
        own=entry["type"].lower() in _OWN_TYPES,
    )


def parse_lldp(device: str, text: str, problems: _Problems) -> Iterator[Neighbor]:
    """`show lldp neighbor`: one block per neighbour, opened by 'Port: 49  Index: 3 ...'."""
    block: dict[str, str] | None = None
    for line in text.splitlines():
        if opened := _LLDP_PORT_RE.match(line):
            if block is not None:
                yield _lldp_neighbor(device, block)
            port = opened.group(1).rstrip(",")
            block = {"port": port} if _PORT_RE.match(port) else None
            if block is None:
                problems.not_read(LLDP, line, "not a port")
            continue
        if block is not None and (field := _LLDP_FIELD_RE.match(line)):
            block[field.group(1)] = field.group(2)
    if block is not None:
        yield _lldp_neighbor(device, block)


def _lldp_neighbor(device: str, block: dict[str, str]) -> Neighbor:
    return Neighbor(
        device=device,
        local_port=block["port"],
        chassis_id=_id_value(block.get("ChassisId")),
        remote_port=_id_value(block.get("PortId")),
        system_name=block.get("SysName") or None,
        capabilities=enabled_capabilities(block.get("SysCap")),
        address=block.get("Address") or None,
    )


def _id_value(printed: str | None) -> str | None:
    """'MAC address    b0:ad:aa:41:b4:df' -> 'b0:ad:aa:41:b4:df'. The subtype and the
    value are separated by a run of spaces; a MAC value is normalised."""
    if not printed:
        return None
    value = re.split(r"\s{2,}", printed, maxsplit=1)[-1].strip()
    return normalize_mac(value) or value


def parse_vlans(text: str) -> dict[int, str]:
    """`show vlan`: '<id> <name with spaces> <type> <protocol> 0x<pid> ...'. The PID is
    the anchor, so a VLAN type this code has never seen still reads."""
    vlans: dict[int, str] = {}
    for line in text.splitlines():
        if (row := _VLAN_ROW_RE.match(line)) and 1 <= int(row.group(1)) <= 4094:
            vlans[int(row.group(1))] = row.group(2).strip()
    return vlans


def parse_ports(device: str, text: str) -> Iterator[PortStatus]:
    """`show interfaces`: '<port> [<trunk>] Enable|Disable Up|Down Up|Down ...'.
    ADMIN is the Enable/Disable word, OPER the first Up/Down after it."""
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or not _PORT_RE.match(tokens[0]):
            continue
        words = [token.lower() for token in tokens]
        admin_at = next((i for i, w in enumerate(words) if w in ("enable", "disable")), None)
        if admin_at is None:
            continue
        oper = next((w for w in words[admin_at + 1 :] if w in ("up", "down")), None)
        if oper is not None:
            yield PortStatus(
                device=device,
                port=tokens[0],
                oper_up=oper == "up",
                admin_up=words[admin_at] == "enable",
            )
