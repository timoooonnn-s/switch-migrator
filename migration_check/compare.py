"""Before vs after, by MAC (decisions 0003-0005)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from migration_check.model import DeviceState, MacSighting, Neighbor, Platform, PortKey, Role
from migration_check.platforms import read_device
from migration_check.roles import assign_roles
from migration_check.snapshot import Snapshot, read_snapshot

LOCAL_ROLES = frozenset({Role.EDGE, Role.ACCESS_POINT})

PortMap = Mapping[PortKey, PortKey]  # old (device, port) -> new (device, port), decision 0010


@dataclass(frozen=True, slots=True)
class LoadedSnapshot:
    snapshot: Snapshot
    devices: tuple[DeviceState, ...]

    @property
    def problems(self) -> tuple[tuple[str, str], ...]:
        """(device, problem) for every device of this snapshot."""
        return tuple(
            (device.name, problem) for device in self.devices for problem in device.problems
        )


def load(
    path: Path,
    *,
    platform: Platform | None = None,
    uplinks: Collection[PortKey] = (),
    edges: Collection[PortKey] = (),
) -> LoadedSnapshot:
    snapshot = read_snapshot(path, default_platform=platform)
    devices = tuple(
        assign_roles(read_device(raw), uplinks=uplinks, edges=edges) for raw in snapshot.devices
    )
    return LoadedSnapshot(snapshot, devices)


class Verdict(StrEnum):
    FOUND = "found"
    OTHER_PORT = "other-port"  # found on an access port, but not where the port map says
    ELSEWHERE = "elsewhere"
    WRONG_VLAN = "wrong-vlan"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class MacResult:
    before: MacSighting  # the latest edge sighting in the baseline
    verdict: Verdict
    expected_vlan: int  # before.vlan_id through the VLAN map
    after: tuple[MacSighting, ...] = ()  # the sightings the verdict is based on
    ip: str | None = None
    neighbor: str | None = None  # the LLDP neighbour on the old port, e.g. a phone
    expected_ports: tuple[PortKey, ...] = ()  # where the port map says it went; () unmapped


@dataclass(frozen=True, slots=True)
class PortCheck:
    """One row of the port map: the old port, where it went, and what came back."""

    old: PortKey
    new: PortKey
    old_up: bool | None  # None: the old device or port is not in the baseline
    new_up: bool | None  # None: the new device or port is not in the after snapshot
    macs_before: int  # edge MACs on the old port
    macs_back: int  # of those, found on the new port

    @property
    def problem(self) -> str | None:
        """What is wrong with the link itself. MACs not back on an up port is a
        per-MAC matter (other-port, missing, ...), not a port problem."""
        if self.new_up is None:
            return "new port not in the after snapshot"
        if not self.new_up:
            return "new port down"
        if self.old_up is None:
            return "old port not in the baseline"
        return None


@dataclass(frozen=True, slots=True)
class VlanGone:
    vlan_id: int
    name: str | None
    edge_macs: int


@dataclass(frozen=True, slots=True)
class Comparison:
    before: tuple[LoadedSnapshot, ...]
    after: LoadedSnapshot
    results: tuple[MacResult, ...]
    new_macs: tuple[MacSighting, ...]
    lost_neighbors: tuple[Neighbor, ...]
    new_neighbors: tuple[Neighbor, ...]
    vlans_gone: tuple[VlanGone, ...]
    vlan_map: Mapping[int, int] = field(default_factory=dict)
    port_checks: tuple[PortCheck, ...] = ()  # only with a port map
    notes: tuple[str, ...] = ()

    def with_verdict(self, verdict: Verdict) -> tuple[MacResult, ...]:
        return tuple(result for result in self.results if result.verdict is verdict)

    @property
    def not_found(self) -> tuple[MacResult, ...]:
        return tuple(result for result in self.results if result.verdict is not Verdict.FOUND)

    @property
    def problems(self) -> tuple[tuple[str, str, str], ...]:
        """(snapshot title, device, problem) across every snapshot read."""
        return tuple(
            (loaded.snapshot.title, device, problem)
            for loaded in (*self.before, self.after)
            for device, problem in loaded.problems
        )

    @property
    def clean(self) -> bool:
        return not (self.not_found or self.vlans_gone or self.problems)


def _chronological(snapshots: Sequence[LoadedSnapshot]) -> list[LoadedSnapshot]:
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(snapshots, key=lambda loaded: loaded.snapshot.taken_at or oldest)


def compare(
    before: Sequence[LoadedSnapshot],
    after: LoadedSnapshot,
    vlan_map: Mapping[int, int] | None = None,
    port_map: PortMap | None = None,
) -> Comparison:
    """Every edge MAC of the baseline (all `before` snapshots merged, the latest
    sighting wins) looked up among every sighting `after` (decision 0005). With a
    port map, a MAC found on another access port than its row says is `other-port`
    (decision 0010)."""
    vlan_map = dict(vlan_map or {})
    port_map = dict(port_map or {})
    before = _chronological(before)
    before_devices = [device for loaded in before for device in loaded.devices]

    baseline: dict[tuple[str, int], MacSighting] = {}
    for device in before_devices:
        for sighting in device.macs:
            if sighting.role is Role.EDGE:
                baseline[(sighting.mac, sighting.vlan_id)] = sighting

    seen_after: dict[str, list[MacSighting]] = defaultdict(list)
    for device in after.devices:
        for sighting in device.macs:
            if sighting.role is not Role.SWITCH:
                seen_after[sighting.mac].append(sighting)

    ip_by_mac: dict[str, str] = {}
    for device in (*before_devices, *after.devices):  # later snapshots win
        ip_by_mac.update(device.ip_by_mac)
    old_neighbors = _neighbors_by_port(before_devices)

    results = tuple(
        _result(
            sighting,
            seen_after.get(sighting.mac, []),
            vlan_map=vlan_map,
            ip_by_mac=ip_by_mac,
            old_neighbors=old_neighbors,
            expected_ports=tuple(port_map[key] for key in _keys(sighting) if key in port_map),
        )
        for sighting in sorted(baseline.values(), key=lambda s: (s.device, s.ports, s.mac))
    )

    baseline_macs = {mac for mac, _ in baseline}
    new_macs = {
        (sighting.mac, sighting.vlan_id): sighting
        for device in after.devices
        for sighting in device.macs
        if sighting.role is Role.EDGE and sighting.mac not in baseline_macs
    }

    lost, new = _neighbor_changes(before_devices, after.devices)
    return Comparison(
        before=tuple(before),
        after=after,
        results=results,
        new_macs=tuple(sorted(new_macs.values(), key=lambda s: (s.device, s.ports, s.mac))),
        lost_neighbors=lost,
        new_neighbors=new,
        vlans_gone=_vlans_gone(results, before_devices, after.devices),
        vlan_map=vlan_map,
        port_checks=_port_checks(port_map, results, before_devices, after.devices),
        notes=_blind_spots(before, after, vlan_map, results, port_map),
    )


def _keys(sighting: MacSighting) -> list[PortKey]:
    return [(sighting.device, port) for port in sighting.ports]


def _result(
    before: MacSighting,
    seen: list[MacSighting],
    *,
    vlan_map: Mapping[int, int],
    ip_by_mac: Mapping[str, str],
    old_neighbors: Mapping[PortKey, list[Neighbor]],
    expected_ports: tuple[PortKey, ...],
) -> MacResult:
    expected = vlan_map.get(before.vlan_id, before.vlan_id)
    local = [s for s in seen if s.role in LOCAL_ROLES and s.vlan_id == expected]
    same_vlan = [s for s in seen if s.vlan_id == expected]
    if local and expected_ports and not any(set(_keys(s)) & set(expected_ports) for s in local):
        verdict, after = Verdict.OTHER_PORT, local
    elif local:
        verdict, after = Verdict.FOUND, local
    elif same_vlan:
        verdict, after = Verdict.ELSEWHERE, same_vlan
    elif seen:
        verdict, after = Verdict.WRONG_VLAN, seen
    else:
        verdict, after = Verdict.MISSING, []
    neighbor = next(
        (
            n.label
            for port in before.ports
            for n in old_neighbors.get((before.device, port), ())
            if not n.is_network_device
        ),
        None,
    )
    return MacResult(
        before=before,
        verdict=verdict,
        expected_vlan=expected,
        after=tuple(after),
        ip=ip_by_mac.get(before.mac),
        neighbor=neighbor,
        expected_ports=expected_ports,
    )


def _port_checks(
    port_map: PortMap,
    results: Sequence[MacResult],
    before: Sequence[DeviceState],
    after: Sequence[DeviceState],
) -> tuple[PortCheck, ...]:
    old_up = {(p.device, p.port): p.oper_up for d in before for p in d.ports}  # latest wins
    new_up = {(p.device, p.port): p.oper_up for d in after for p in d.ports}
    checks = []
    for old, new in sorted(port_map.items()):
        on_old_port = [r for r in results if old in _keys(r.before)]
        back = [r for r in on_old_port if any(new in _keys(s) for s in r.after)]
        checks.append(
            PortCheck(old, new, old_up.get(old), new_up.get(new), len(on_old_port), len(back))
        )
    return tuple(checks)


def _neighbors_by_port(devices: Sequence[DeviceState]) -> dict[PortKey, list[Neighbor]]:
    by_port: dict[PortKey, list[Neighbor]] = defaultdict(list)
    for device in devices:
        for neighbor in device.neighbors:
            by_port[(neighbor.device, neighbor.local_port)].append(neighbor)
    return by_port


def _neighbor_changes(
    before: Sequence[DeviceState], after: Sequence[DeviceState]
) -> tuple[tuple[Neighbor, ...], tuple[Neighbor, ...]]:
    """Neighbours keyed by what they advertise, not where they are plugged in (0003)."""
    old = {n.identity: n for device in before for n in device.neighbors}
    new = {n.identity: n for device in after for n in device.neighbors}
    lost = tuple(neighbor for key, neighbor in old.items() if key not in new)
    added = tuple(neighbor for key, neighbor in new.items() if key not in old)
    return lost, added


def _vlans_gone(
    results: Sequence[MacResult],
    before: Sequence[DeviceState],
    after: Sequence[DeviceState],
) -> tuple[VlanGone, ...]:
    vlans_after = {vlan for device in after for vlan in device.vlans}
    if not vlans_after:
        return ()  # no VLAN table read after: already reported as a problem
    names = {vlan: name for device in before for vlan, name in device.vlans.items()}
    missing: dict[int, int] = defaultdict(int)
    for result in results:
        if result.expected_vlan not in vlans_after:
            missing[result.expected_vlan] += 1
    return tuple(VlanGone(vlan, names.get(vlan), count) for vlan, count in sorted(missing.items()))


def _blind_spots(
    before: Sequence[LoadedSnapshot],
    after: LoadedSnapshot,
    vlan_map: Mapping[int, int],
    results: Sequence[MacResult],
    port_map: PortMap,
) -> tuple[str, ...]:
    """What this comparison cannot see, printed in every report (decision 0007)."""
    notes = [
        "A device that has not sent a frame since the change is not in any MAC table yet: "
        "it shows as missing until it does. Run the comparison again in a few minutes.",
        "NAC: a device that fails authentication may appear in another VLAN, or not at all.",
    ]
    elan = sum(device.elan_endpoints for device in after.devices)
    if elan:
        notes.append(
            f"{elan} ELAN / Switched UNI endpoint(s) after the change: MACs behind them are "
            "not in the VLAN MAC table and may show as missing."
        )
    if vlan_map:
        pairs = ", ".join(f"{old}->{new}" for old, new in sorted(vlan_map.items()))
        notes.append(f"VLANs translated before comparing: {pairs}.")
    if port_map:
        unmapped = {r.before.location for r in results if not r.expected_ports}
        if unmapped:
            notes.append(
                f"{len(unmapped)} old port(s) with edge MACs are not in the port map: "
                "their MACs are checked across all switches, not against a new port."
            )
    for loaded in (*before, after):
        notes.extend(f"{loaded.snapshot.title}: {note}" for note in loaded.snapshot.notes)
    return tuple(notes)
