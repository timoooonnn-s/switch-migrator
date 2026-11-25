from __future__ import annotations

import pytest

from migration_check.model import DeviceState, MacSighting, Neighbor, Platform, PortKey, Role
from migration_check.roles import assign_roles

AP_MAC = "00:00:00:00:0a:01"


def _neighbor(port: str, capabilities: str, chassis: str = "00:00:00:00:ff:01") -> Neighbor:
    return Neighbor("sw", port, chassis, "eth0", f"n-{port}", capabilities)


def _role(
    sighting: MacSighting,
    neighbors: tuple[Neighbor, ...] = (),
    uplinks: tuple[PortKey, ...] = (),
    edges: tuple[PortKey, ...] = (),
) -> Role:
    state = DeviceState("sw", Platform.BOSS, macs=(sighting,), neighbors=neighbors)
    return assign_roles(state, uplinks=uplinks, edges=edges).macs[0].role


def _on(port: str, mac: str = "00:00:00:00:00:01", **kwargs: object) -> MacSighting:
    return MacSighting("sw", mac, 10, f"Port:{port}", (port,), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("capabilities", "role"),
    [
        ("", Role.EDGE),  # a PC without LLDP
        ("BT", Role.EDGE),  # an IP phone bridges for the PC behind it
        ("S", Role.EDGE),
        ("rB", Role.UPLINK),  # the DvR controllers in switch-migrator's fixture
        ("Br", Role.UPLINK),  # Fabric Engine neighbours
        ("B", Role.UPLINK),
        ("BW", Role.ACCESS_POINT),
    ],
)
def test_role_from_lldp_capabilities(capabilities: str, role: Role) -> None:
    assert _role(_on("7"), (_neighbor("7", capabilities),)) is role


def test_the_access_points_own_mac_is_an_edge_mac() -> None:
    neighbors = (_neighbor("7", "BW", chassis=AP_MAC),)
    assert _role(_on("7", mac=AP_MAC), neighbors) is Role.EDGE
    assert _role(_on("7"), neighbors) is Role.ACCESS_POINT


def test_one_mlt_member_towards_a_switch_makes_the_mlt_an_uplink() -> None:
    sighting = MacSighting("sw", "00:00:00:00:00:01", 10, "Trunk:1", ("49", "50"), mlt_id=1)
    assert _role(sighting, (_neighbor("50", "rB"),)) is Role.UPLINK


def test_own_fabric_and_unresolved() -> None:
    assert _role(_on("7", own=True)) is Role.SWITCH
    assert _role(_on("1/2", remote_beb="fe-03")) is Role.FABRIC
    assert _role(MacSighting("sw", "00:00:00:00:00:01", 10, "MLT-gone", ())) is Role.UNKNOWN


def test_overrides_win_over_lldp() -> None:
    hypervisor = (_neighbor("7", "B"),)  # a vSwitch advertising Bridge
    assert _role(_on("7"), hypervisor, edges=(("sw", "7"),)) is Role.EDGE
    assert _role(_on("8"), uplinks=(("sw", "8"),)) is Role.UPLINK
