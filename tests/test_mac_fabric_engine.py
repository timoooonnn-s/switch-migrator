from __future__ import annotations

from mac_check_samples import FE_LAB2
from migration_check.compare import load
from migration_check.model import Platform, Role


def test_lab2_reads_without_problems() -> None:
    loaded = load(FE_LAB2)
    assert [d.name for d in loaded.devices] == ["fe-01", "fe-02", "fe-03", "fe-04"]
    assert {d.platform for d in loaded.devices} == {Platform.FABRIC_ENGINE}
    assert loaded.problems == ()


def test_lab2_fe04_macs_roles_and_arp() -> None:
    fe04 = load(FE_LAB2).devices[3]
    learned = sorted(
        (m.mac, m.vlan_id, m.ports, m.remote_beb, m.role) for m in fe04.macs if not m.own
    )
    assert learned == [
        ("00:50:79:66:68:01", 10, ("1/3",), None, Role.EDGE),
        ("00:50:79:66:68:08", 104, ("1/6",), None, Role.EDGE),
        # learned through the fabric from fe-03: not this switch's end device
        ("0c:03:90:b8:01:02", 10, ("1/2",), "fe-03", Role.FABRIC),
    ]


def test_arp_comes_from_the_gateway_not_the_access_switch() -> None:
    devices = {d.name: d for d in load(FE_LAB2).devices}
    assert "00:50:79:66:68:01" not in devices["fe-04"].ip_by_mac
    assert devices["fe-03"].ip_by_mac["00:50:79:66:68:01"] == "10.10.10.14"


def test_backbone_vlans_are_neither_macs_nor_vlans() -> None:
    backbone = {4051, 4052}  # spbm-bvlan in lab2; 4048 is the onboarding VLAN and stays
    for device in load(FE_LAB2).devices:
        assert not {m.vlan_id for m in device.macs} & backbone
        assert not set(device.vlans) & backbone
        assert 4048 in device.vlans


def test_elan_endpoints_are_counted() -> None:
    # lab2: ELAN I-SID 30100 on fe-01 (fabric-weft tests/fixtures/README.md)
    counts = {d.name: d.elan_endpoints for d in load(FE_LAB2).devices}
    assert counts["fe-01"] >= 1
    assert counts["fe-04"] == 0
