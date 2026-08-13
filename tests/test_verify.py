"""Post-migration verification: did every link come back up where it should?"""

import csv
from pathlib import Path

import pytest

from switch_migrator import cabling_sheet as CS
from switch_migrator import verify
from switch_migrator.models import Platform, PortState, SwitchAudit

HEADERS = ["Port ID", "Type", "End device / neighbor", "NEW switch", "NEW port",
           "NEW MLT ID", "NEW VLAN", "Old switch", "Old port", "MLT ID",
           "MLT VLANs", "Port VLANs", "MAC addresses", "Usage"]


def _sheet(tmp_path: Path, rows: list[dict]) -> CS.Sheet:
    path = tmp_path / "cabling.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        for r in rows:
            w.writerow([r.get(h, "") for h in HEADERS])
    return CS.load(path)


def _leaf(ports: list[PortState], name: str = "leaf-01",
          reachable: bool = True) -> SwitchAudit:
    a = SwitchAudit(name=name, host=name, platform=Platform.VOSS,
                    reachable=reachable)
    a.ports = ports
    return a


def _one(tmp_path, row: dict, ports: list[PortState], **kw):
    sheet = _sheet(tmp_path, [row])
    report = verify.verify(sheet, [_leaf(ports, **kw)])
    return report.verdicts[0]


# --------------------------- the pass rule ---------------------------------

def test_link_up_and_an_expected_mac_is_a_pass(tmp_path):
    """The strongest evidence available that the right cable went into the
    right hole: the same machine is talking on the new port."""
    v = _one(tmp_path,
             {"Port ID": "P0001", "Old switch": "gx-01", "Old port": "1/7",
              "NEW switch": "leaf-01", "NEW port": "1/9",
              "MAC addresses": "00:11:22:33:44:55,00:11:22:33:44:56"},
             [PortState(port="1/9", oper_up=True,
                        macs=["00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"])])
    assert v.result == verify.PASS
    assert v.macs_found == 1 and v.macs_expected == 2
    assert "1 of 2 expected MAC(s)" in v.why


def test_link_up_but_no_mac_yet_is_a_warn_not_a_failure(tmp_path):
    """A host that has not sent a frame since the cutover looks exactly like a
    mis-patch. Neither is proven, so neither verdict is given."""
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True)])
    assert v.result == verify.WARN
    assert "re-run in a few minutes" in v.why


def test_a_down_link_fails(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=False, state_reason="SSH")])
    assert v.result == verify.FAIL
    assert "DOWN" in v.why and "SSH" in v.why


def test_a_port_that_does_not_exist_fails(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/99"},
             [PortState(port="1/9", oper_up=True)])
    assert v.result == verify.FAIL
    assert "does not exist" in v.why and v.link == "not found"


def test_an_unreachable_new_switch_fails(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9"},
             [], reachable=False)
    assert v.result == verify.FAIL and v.link == "unreachable"


def test_a_switch_that_was_never_collected_fails_with_advice(tmp_path):
    sheet = _sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-99",
         "NEW port": "1/9"}])
    report = verify.verify(sheet, [_leaf([PortState(port="1/9", oper_up=True)])])
    assert report.verdicts[0].result == verify.FAIL
    assert "not collected" in report.verdicts[0].why
    assert any("leaf-99" in p for p in report.problems)


def test_an_unfilled_row_is_pending_not_a_failure(tmp_path):
    sheet = _sheet(tmp_path, [{"Old switch": "gx-01", "Old port": "1/7"}])
    report = verify.verify(sheet, [_leaf([])])
    assert report.verdicts[0].result == verify.PENDING
    assert report.ok                      # pending links do not fail the run


# --------------------------- the softer signals ----------------------------

def test_the_lldp_neighbor_can_stand_in_for_a_missing_mac(tmp_path):
    """The old port learned nothing, but the far end announces itself on the
    new port - that identifies the link just as well."""
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "End device / neighbor": "srv-esx-01"},
             [PortState(port="1/9", oper_up=True, lldp_neighbor="srv-esx-01")])
    assert v.result == verify.PASS
    assert "LLDP neighbor matches" in v.why


def test_the_wrong_neighbor_downgrades_a_mac_pass_to_a_warn(tmp_path):
    """A MAC can be learned through an intermediate switch; a different device
    announcing itself on the port is worth a human look either way."""
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "End device / neighbor": "srv-esx-01",
              "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:33:44:55"],
                        lldp_neighbor="srv-esx-99")])
    assert v.result == verify.WARN
    assert "srv-esx-99" in v.why and "srv-esx-01" in v.why


def test_neighbor_identity_is_matched_loosely(tmp_path):
    """The sheet records whatever identified the device best - a name, an IP,
    or a SysDescr. The new switch may report a different one of the three."""
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9",
              "End device / neighbor": "HPE ProLiant DL380 Gen10"},
             [PortState(port="1/9", oper_up=True,
                        lldp_neighbor="HPE ProLiant DL380 Gen10 iLO")])
    assert v.result == verify.PASS


def test_a_missing_expected_vlan_is_a_warn(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "Port VLANs": "695,735",
              "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:33:44:55"],
                        vlans=[695])])
    assert v.result == verify.WARN
    assert "735 not on the new port" in v.why


def test_the_planners_new_vlan_is_what_gets_checked(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "Port VLANs": "695", "NEW VLAN": "800",
              "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:33:44:55"],
                        vlans=[800])])
    assert v.result == verify.PASS and v.vlans_expected == [800]


def test_wrong_mlt_membership_is_a_warn(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "NEW MLT ID": "35",
              "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:33:44:55"],
                        mlt_id=36)])
    assert v.result == verify.WARN
    assert "is in MLT 36" in v.why and "sheet says MLT 35" in v.why


def test_a_port_missing_from_its_mlt_is_a_warn(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "NEW MLT ID": "35",
              "MAC addresses": "00:11:22:33:44:55"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:33:44:55"])])
    assert v.result == verify.WARN and "not in any MLT" in v.why


def test_mac_comparison_ignores_case_and_separator(tmp_path):
    v = _one(tmp_path,
             {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
              "NEW port": "1/9", "MAC addresses": "00:11:22:AA:BB:CC"},
             [PortState(port="1/9", oper_up=True, macs=["00:11:22:aa:bb:cc"])])
    assert v.result == verify.PASS


# --------------------------- report level ----------------------------------

def test_counts_and_exit_state(tmp_path):
    sheet = _sheet(tmp_path, [
        {"Port ID": "P1", "Old switch": "gx-01", "Old port": "1/1",
         "NEW switch": "leaf-01", "NEW port": "1/1",
         "MAC addresses": "00:11:22:33:44:01"},
        {"Port ID": "P2", "Old switch": "gx-01", "Old port": "1/2",
         "NEW switch": "leaf-01", "NEW port": "1/2",
         "MAC addresses": "00:11:22:33:44:02"},
        {"Port ID": "P3", "Old switch": "gx-01", "Old port": "1/3",
         "NEW switch": "leaf-01", "NEW port": "1/3"},
        {"Port ID": "P4", "Old switch": "gx-01", "Old port": "1/4"},
    ])
    report = verify.verify(sheet, [_leaf([
        PortState(port="1/1", oper_up=True, macs=["00:11:22:33:44:01"]),
        PortState(port="1/2", oper_up=False),
        PortState(port="1/3", oper_up=True),
    ])])
    assert report.counts() == {verify.PASS: 1, verify.WARN: 1,
                               verify.FAIL: 1, verify.PENDING: 1}
    assert not report.ok            # a FAIL means the run is not clean
    assert report.verdicts[0].result == verify.FAIL    # worst first


def test_ports_up_that_are_in_no_sheet_row_are_surfaced(tmp_path):
    """The other direction: a link somebody patched without writing it down."""
    sheet = _sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/1", "NEW switch": "leaf-01",
         "NEW port": "1/1"}])
    audits = [_leaf([
        PortState(port="1/1", oper_up=True),
        PortState(port="1/2", oper_up=True),      # unlisted, up
        PortState(port="1/3", oper_up=False),     # unlisted, down: not news
    ])]
    assert verify.unexpected_ports(sheet, audits) == [("leaf-01", "1/2")]


def test_switches_not_named_in_the_sheet_are_not_scanned_for_extras(tmp_path):
    sheet = _sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/1", "NEW switch": "leaf-01",
         "NEW port": "1/1"}])
    audits = [_leaf([PortState(port="1/1", oper_up=True)]),
              _leaf([PortState(port="1/5", oper_up=True)], name="core-01")]
    assert verify.unexpected_ports(sheet, audits) == []


def test_sheet_row_problems_reach_the_report(tmp_path):
    sheet = _sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/1", "NEW switch": "leaf-01",
         "NEW port": "1/1", "NEW MLT ID": "later"}])
    report = verify.verify(sheet, [_leaf([PortState(port="1/1", oper_up=True)])])
    assert any("not a number" in p for p in report.problems)
