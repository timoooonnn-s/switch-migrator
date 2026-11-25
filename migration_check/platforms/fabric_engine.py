"""Fabric Engine / VOSS: raw output -> DeviceState, through fabric-weft (decision 0006)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fabric_weft import (
    ArpEntry,
    DeviceError,
    LldpNeighbor,
    MacEntry,
    Mlt,
    ParseResult,
    PortIsid,
    PortName,
    PortState,
    Vlan,
    parse_arp_entries,
    parse_lldp_neighbors,
    parse_mac_entries,
    parse_mlts,
    parse_port_isids,
    parse_port_names,
    parse_port_states,
    parse_vlans,
)

from migration_check.model import DeviceState, MacSighting, Neighbor, Platform, PortStatus
from migration_check.platforms.common import enabled_capabilities
from migration_check.snapshot import RawDevice

MAC_TABLE = "show vlan mac-address-entry"
LLDP = "show lldp neighbor"
MLTS = "show mlt"
VLANS = "show vlan basic"
PORT_STATES = "show interfaces gigabitEthernet state"
PORT_NAMES = "show interfaces gigabitEthernet name"
PORT_ISIDS = "show interfaces gigabitEthernet i-sid"
ARP = "show ip arp"

COMMANDS = (MAC_TABLE, LLDP, MLTS, VLANS, PORT_STATES, PORT_NAMES, PORT_ISIDS, ARP)
REQUIRED = frozenset({MAC_TABLE, LLDP, MLTS, VLANS})
NETMIKO_DEVICE_TYPE = "extreme_vsp"

RecordT = TypeVar("RecordT")


def read_device(raw: RawDevice) -> DeviceState:
    problems: list[str] = []

    def records(
        command: str, parser: Callable[..., ParseResult[Any]], record_type: type[RecordT]
    ) -> tuple[RecordT, ...]:
        text = raw.output(command)
        if text is None:
            if command in REQUIRED:
                problems.append(raw.why_missing(command))
            return ()
        try:
            result = parser(text, command=command)
        except DeviceError as error:
            problems.append(str(error))
            return ()
        if result.skipped:
            first = result.skipped[0]
            problems.append(
                f"{command!r}: {len(result.skipped)} line(s) not read, "
                f"first: {first.line.strip()!r} ({first.reason})"
            )
        return tuple(record for record in result if isinstance(record, record_type))

    vlans = records(VLANS, parse_vlans, Vlan)
    mlts = records(MLTS, parse_mlts, Mlt)
    backbone_vlans = {vlan.vlan_id for vlan in vlans if "bvlan" in vlan.vlan_type.lower()}
    macs = tuple(
        _sighting(raw.name, entry, mlts)
        for entry in records(MAC_TABLE, parse_mac_entries, MacEntry)
        # B-VLAN entries are the fabric's own backbone MACs, never an end device
        if entry.vlan_id not in backbone_vlans
    )
    return DeviceState(
        name=raw.name,
        platform=Platform.FABRIC_ENGINE,
        macs=macs,
        neighbors=tuple(
            _neighbor(raw.name, lldp) for lldp in records(LLDP, parse_lldp_neighbors, LldpNeighbor)
        ),
        ports=_ports(
            raw.name,
            records(PORT_STATES, parse_port_states, PortState),
            records(PORT_NAMES, parse_port_names, PortName),
        ),
        vlans={vlan.vlan_id: vlan.name for vlan in vlans if vlan.vlan_id not in backbone_vlans},
        ip_by_mac={arp.mac: arp.ip_address for arp in records(ARP, parse_arp_entries, ArpEntry)},
        elan_endpoints=sum(
            1
            for endpoint in records(PORT_ISIDS, parse_port_isids, PortIsid)
            if endpoint.isid_type.upper().startswith("ELAN")
        ),
        problems=tuple(problems),
    )


def _sighting(device: str, entry: MacEntry, mlts: tuple[Mlt, ...]) -> MacSighting:
    own = entry.status.lower() == "self" or "cpp" in entry.interface.lower()
    if entry.port is not None:
        return MacSighting(
            device=device,
            mac=entry.mac,
            vlan_id=entry.vlan_id,
            interface=entry.interface,
            ports=(entry.port,),
            remote_beb=entry.tunnel,
            own=own,
        )
    # An MLT-learned entry prints the MLT's *name* in INTERFACE (real 8.10 output,
    # per switch-migrator), so it is matched against `show mlt` by name.
    mlt = next((m for m in mlts if m.name == entry.interface), None)
    return MacSighting(
        device=device,
        mac=entry.mac,
        vlan_id=entry.vlan_id,
        interface=entry.interface,
        ports=mlt.members if mlt else (),
        mlt_id=mlt.mlt_id if mlt else None,
        remote_beb=entry.tunnel,
        own=own,
    )


def _neighbor(device: str, lldp: LldpNeighbor) -> Neighbor:
    return Neighbor(
        device=device,
        local_port=lldp.local_port,
        chassis_id=lldp.chassis_id,
        remote_port=lldp.port_id,
        system_name=lldp.system_name,
        capabilities=enabled_capabilities(lldp.system_capabilities),
        address=lldp.address,
    )


def _ports(
    device: str, states: tuple[PortState, ...], names: tuple[PortName, ...]
) -> tuple[PortStatus, ...]:
    name_by_port = {name.port: name for name in names}
    if states:
        return tuple(
            PortStatus(
                device=device,
                port=state.port,
                oper_up=state.oper_up,
                admin_up=state.admin_up,
                name=name_by_port[state.port].name if state.port in name_by_port else None,
            )
            for state in states
        )
    return tuple(
        PortStatus(device=device, port=name.port, oper_up=name.oper_up, name=name.name)
        for name in names
    )
