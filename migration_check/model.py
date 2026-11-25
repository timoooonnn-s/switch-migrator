"""The OS-neutral records every platform adapter produces (decisions 0003, 0004).

A Fabric Engine switch and a BOSS stack print the same facts in different shapes. Each
adapter in `platforms/` reads one shape; everything after that (roles, the comparison,
the report) only ever sees these records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

PortKey = tuple[str, str]  # (device, port): a port anywhere in the scope


class Platform(StrEnum):
    FABRIC_ENGINE = "fabric-engine"
    BOSS = "boss"

    @classmethod
    def from_name(cls, name: str) -> Platform:
        """Accepts switch-migrator's inventory names ('voss', 'ers') as well."""
        normalized = name.strip().lower().replace("_", "-")
        for platform, aliases in _PLATFORM_ALIASES.items():
            if normalized in aliases:
                return platform
        known = ", ".join(
            sorted(alias for aliases in _PLATFORM_ALIASES.values() for alias in aliases)
        )
        raise ValueError(f"unknown platform {name!r} (known: {known})")


_PLATFORM_ALIASES = {
    Platform.FABRIC_ENGINE: {"fabric-engine", "fe", "voss", "vsp"},
    Platform.BOSS: {"boss", "ers"},
}


class Role(StrEnum):
    """Where a MAC was learned, and so whether it is compared (decision 0004)."""

    EDGE = "edge"
    ACCESS_POINT = "access-point"
    UPLINK = "uplink"
    FABRIC = "fabric"
    SWITCH = "switch"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Neighbor:
    """One LLDP neighbour, as the device it belongs to saw it."""

    device: str
    local_port: str
    chassis_id: str | None
    remote_port: str | None
    system_name: str | None
    capabilities: str  # the *enabled* capability letters, e.g. 'rB'; '' when not advertised
    address: str | None = None

    @property
    def is_access_point(self) -> bool:
        return "W" in self.capabilities

    @property
    def is_network_device(self) -> bool:
        """A switch or router. Phones and APs advertise Bridge too, so they are excluded."""
        forwards = any(letter in self.capabilities for letter in "BrR")
        return forwards and not any(letter in self.capabilities for letter in "TW")

    @property
    def identity(self) -> tuple[str, str]:
        """What the neighbour is, independent of where it is plugged in (decision 0003)."""
        return (self.chassis_id or self.system_name or "?", self.remote_port or "?")

    @property
    def label(self) -> str:
        return self.system_name or self.chassis_id or self.address or "?"


@dataclass(frozen=True, slots=True)
class MacSighting:
    """One forwarding-database entry: a MAC in a VLAN, learned on an interface."""

    device: str
    mac: str  # aa:bb:cc:dd:ee:ff
    vlan_id: int
    interface: str  # as printed: 'Port-1/7', 'Port:7', 'Trunk:1', 'MLT035.s.1/2/23/24'
    ports: tuple[str, ...]  # front ports, MLT members expanded; () for the CPU or unresolved
    mlt_id: int | None = None
    remote_beb: str | None = None  # Fabric Engine TUNNEL: learned through the fabric
    own: bool = False  # the switch's own MAC
    role: Role = Role.UNKNOWN  # set by `roles.assign_roles`, never by an adapter

    @property
    def location(self) -> str:
        if self.remote_beb:
            return f"{self.device} via {self.remote_beb}"
        if self.mlt_id is not None:
            return f"{self.device} MLT {self.mlt_id} ({','.join(self.ports) or '-'})"
        if self.ports:
            return f"{self.device} port {','.join(self.ports)}"
        return f"{self.device} {self.interface}"


@dataclass(frozen=True, slots=True)
class PortStatus:
    device: str
    port: str
    oper_up: bool
    admin_up: bool | None = None  # None: not printed by the command read
    name: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceState:
    """Everything one device's raw output says, in OS-neutral records."""

    name: str
    platform: Platform
    macs: tuple[MacSighting, ...] = ()
    neighbors: tuple[Neighbor, ...] = ()
    ports: tuple[PortStatus, ...] = ()
    vlans: Mapping[int, str] = field(default_factory=dict)  # VLAN ID -> name
    ip_by_mac: Mapping[str, str] = field(default_factory=dict)  # from ARP, where read
    elan_endpoints: int = 0  # Fabric Engine ELAN / Switched UNI endpoints (decision 0007)
    problems: tuple[str, ...] = ()  # rejected commands, missing files, unread lines

    @property
    def ports_up(self) -> int:
        return sum(1 for port in self.ports if port.oper_up)
