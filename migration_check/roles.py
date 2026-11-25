"""Which MACs are end devices and which are only passing through (decision 0004)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import replace

from migration_check.model import DeviceState, MacSighting, Neighbor, PortKey, Role


def assign_roles(
    state: DeviceState,
    uplinks: Collection[PortKey] = (),
    edges: Collection[PortKey] = (),
) -> DeviceState:
    neighbors_by_port: dict[str, list[Neighbor]] = defaultdict(list)
    for neighbor in state.neighbors:
        neighbors_by_port[neighbor.local_port].append(neighbor)
    macs = tuple(
        replace(sighting, role=_role(sighting, neighbors_by_port, uplinks, edges))
        for sighting in state.macs
    )
    return replace(state, macs=macs)


def _role(
    sighting: MacSighting,
    neighbors_by_port: dict[str, list[Neighbor]],
    uplinks: Collection[PortKey],
    edges: Collection[PortKey],
) -> Role:
    if sighting.own:
        return Role.SWITCH
    if sighting.remote_beb:
        return Role.FABRIC
    if not sighting.ports:
        return Role.UNKNOWN
    refs = {(sighting.device, port) for port in sighting.ports}
    if refs & set(edges):
        return Role.EDGE
    if refs & set(uplinks):
        return Role.UPLINK
    neighbors = [n for port in sighting.ports for n in neighbors_by_port.get(port, ())]
    if any(neighbor.is_network_device for neighbor in neighbors):
        return Role.UPLINK
    access_points = [neighbor for neighbor in neighbors if neighbor.is_access_point]
    if access_points and all(ap.chassis_id != sighting.mac for ap in access_points):
        return Role.ACCESS_POINT  # a wireless client; the AP's own MAC stays an edge MAC
    return Role.EDGE
