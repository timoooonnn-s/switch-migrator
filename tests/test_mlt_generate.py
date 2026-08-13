"""Generating the new switches' MLT blocks from a filled-in cabling sheet."""

import csv
from pathlib import Path

from switch_migrator import cabling_sheet as CS
from switch_migrator import mlt_generate as G

HEADERS = ["Port ID", "Type", "End device / neighbor", "NEW switch", "NEW port",
           "NEW MLT ID", "NEW MLT name", "NEW VLAN", "Old switch", "Old port",
           "MLT ID", "MLT name", "MLT VLANs", "Port VLANs", "Port I-SIDs"]


def _sheet(tmp_path: Path, rows: list[dict]) -> CS.Sheet:
    path = tmp_path / "cabling.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        for r in rows:
            w.writerow([r.get(h, "") for h in HEADERS])
    return CS.load(path)


def _member(port: str, new_port: str, **kw) -> dict:
    row = {"Old switch": "gx-01", "Old port": port, "NEW switch": "leaf-01",
           "NEW port": new_port, "MLT ID": "35", "MLT name": "MLT035.srv",
           "Type": "mlt"}
    row.update(kw)
    return row


def test_an_old_mlt_becomes_one_block_with_its_members(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11"), _member("1/2", "1/12")]))
    assert len(result.plans) == 1
    p = result.plans[0]
    assert (p.switch, p.mlt_id, p.name) == ("leaf-01", 35, "MLT035.srv")
    assert p.members == ["1/11", "1/12"]
    assert p.smlt and p.flex_uni
    assert "carried over from gx-01" in p.id_source
    assert p.problems == []


def test_the_sheets_new_id_and_name_win(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"NEW MLT ID": "77", "NEW MLT name": "srv-lag"}),
        _member("1/2", "1/12", **{"NEW MLT ID": "77", "NEW MLT name": "srv-lag"}),
    ]))
    p = result.plans[0]
    assert (p.mlt_id, p.name) == (77, "srv-lag")
    assert p.id_source == "sheet"
    assert "was: gx-01 MLT 35" in G.render(result)


def test_a_new_id_on_only_some_rows_still_groups_them(tmp_path):
    """Half-filled is the normal state of a sheet mid-window; the id the
    planner did write is the one that counts."""
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"NEW MLT ID": "77"}),
        _member("1/2", "1/12", **{"NEW MLT ID": "77", "NEW MLT name": "srv-lag"}),
    ]))
    assert len(result.plans) == 1
    assert result.plans[0].name == "srv-lag"


def test_two_old_mlts_consolidated_onto_one_new_id(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"NEW MLT ID": "77"}),
        _member("1/2", "1/12", **{"NEW MLT ID": "77"}),
        {"Old switch": "gx-02", "Old port": "1/1", "NEW switch": "leaf-01",
         "NEW port": "1/13", "MLT ID": "36", "NEW MLT ID": "77", "Type": "mlt"},
    ]))
    p = result.plans[0]
    assert p.members == ["1/11", "1/12", "1/13"]
    assert any("consolidates 2 old MLTs" in x for x in p.problems)


def test_a_plain_access_port_is_not_an_mlt(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/7", "NEW switch": "leaf-01",
         "NEW port": "1/7", "Type": "access"}]))
    assert result.plans == []


def test_an_aggregation_of_one_is_flagged(tmp_path):
    result = G.plan(_sheet(tmp_path, [_member("1/1", "1/11")]))
    assert any("only one member" in x for x in result.plans[0].problems)


def test_an_id_claimed_twice_on_one_switch_is_a_problem(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11"), _member("1/2", "1/12"),
        {"Old switch": "gx-02", "Old port": "1/1", "NEW switch": "leaf-01",
         "NEW port": "1/13", "MLT ID": "35", "Type": "mlt"},
        {"Old switch": "gx-02", "Old port": "1/2", "NEW switch": "leaf-01",
         "NEW port": "1/14", "MLT ID": "35", "Type": "mlt"},
    ]))
    assert any("claimed twice" in p for p in result.problems)
    assert any("Set a NEW MLT ID" in p for p in result.problems)


def test_two_neighbors_on_an_uplink_is_the_normal_smlt_shape(tmp_path):
    """An uplink to a vIST pair legitimately sees two different LLDP
    neighbors - that must not be reported as a fault."""
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/49", **{"Type": "uplink",
                                  "End device / neighbor": "dvr-01"}),
        _member("1/2", "1/50", **{"Type": "uplink",
                                  "End device / neighbor": "dvr-02"}),
    ]))
    assert result.plans[0].problems == []


def test_two_neighbors_on_a_server_lag_is_worth_a_note(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"End device / neighbor": "srv-a"}),
        _member("1/2", "1/12", **{"End device / neighbor": "srv-b"}),
    ]))
    assert any("vIST/SMLT pair" in x for x in result.plans[0].problems)


def test_three_neighbors_cannot_be_a_pair_and_is_a_fault(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"End device / neighbor": "srv-a"}),
        _member("1/2", "1/12", **{"End device / neighbor": "srv-b"}),
        _member("1/3", "1/13", **{"End device / neighbor": "srv-c"}),
    ]))
    assert any("must terminate on ONE device" in x
               for x in result.plans[0].problems)


def test_an_smlt_on_only_one_peer_is_called_out(tmp_path):
    """The failure mode this exists for: an SMLT configured on one peer only
    is a single point of failure that looks redundant."""
    rows = []
    for leaf in ("leaf-01", "leaf-02"):        # MLT 35 correctly on both peers
        rows += [_member("1/1", "1/11", **{"NEW switch": leaf}),
                 _member("1/2", "1/12", **{"NEW switch": leaf})]
    rows += [                                   # MLT 36 only on leaf-01
        {"Old switch": "gx-01", "Old port": "1/3", "NEW switch": "leaf-01",
         "NEW port": "1/13", "MLT ID": "36", "Type": "mlt"},
        {"Old switch": "gx-01", "Old port": "1/4", "NEW switch": "leaf-01",
         "NEW port": "1/14", "MLT ID": "36", "Type": "mlt"},
    ]
    result = G.plan(_sheet(tmp_path, rows))
    assert any("MLT 36 exists on only one new switch" in p
               for p in result.problems)
    assert not any("MLT 35 exists on only one" in p for p in result.problems)


def test_a_single_leaf_migration_does_not_complain_about_pairing(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11"), _member("1/2", "1/12")]))
    assert not any("only one new switch" in p for p in result.problems)


def test_an_unfilled_sheet_says_what_to_do(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        {"Old switch": "gx-01", "Old port": "1/1", "MLT ID": "35"}]))
    assert result.plans == []
    assert any("fill those in" in p for p in result.problems)


# --------------------------- rendering -------------------------------------

def test_the_emitted_block_matches_the_devices_own_syntax(tmp_path):
    """Both sections a VOSS box prints in its own running-config: the MLT
    definition, then the interface block."""
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"Port VLANs": "695", "Port I-SIDs": "2500695"}),
        _member("1/2", "1/12", **{"Port VLANs": "695", "Port I-SIDs": "2500695"}),
    ]))
    text = G.render(result, source="cabling.xlsx")
    assert 'mlt 35 enable name "MLT035.srv"' in text
    assert "mlt 35 member 1/11,1/12" in text
    assert "interface mlt 35" in text
    assert "smlt" in text
    assert "lacp enable key 35" in text
    assert "flex-uni enable" in text
    assert text.count("exit") == 1
    # the context a human needs to check it against reality
    assert "carried VLAN(s): 695" in text and "I-SID(s): 2500695" in text
    assert "REVIEW BEFORE PASTING" in text


def test_no_service_configuration_is_ever_invented(tmp_path):
    """I-SIDs and c-vids come from the extract/worksheet, which have their own
    rules about guessing. This file must not contain any."""
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11", **{"Port I-SIDs": "2500695"}),
        _member("1/2", "1/12", **{"Port I-SIDs": "2500695"}),
    ]))
    for line in G.render(result).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        assert not line.startswith(("i-sid", "c-vid", "untagged-traffic",
                                    "vlan ")), line


def test_no_smlt_emits_a_plain_mlt(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11"), _member("1/2", "1/12")]), smlt=False)
    text = G.render(result)
    assert "interface mlt 35" in text
    assert "\nsmlt\n" not in text


def test_blocks_are_grouped_per_new_switch(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/11"), _member("1/2", "1/12"),
        _member("1/1", "1/11", **{"NEW switch": "leaf-02"}),
        _member("1/2", "1/12", **{"NEW switch": "leaf-02"}),
    ]))
    text = G.render(result)
    assert "===== leaf-01 =====" in text and "===== leaf-02 =====" in text
    assert text.index("===== leaf-01 =====") < text.index("===== leaf-02 =====")


def test_problems_are_printed_before_the_config(tmp_path):
    result = G.plan(_sheet(tmp_path, [_member("1/1", "1/11")]))
    text = G.render(result)
    assert "[!] only one member" in text
    assert text.index("[!]") < text.index("mlt 35 enable")


def test_members_are_ordered_numerically_not_alphabetically(tmp_path):
    result = G.plan(_sheet(tmp_path, [
        _member("1/1", "1/2"), _member("1/2", "1/10"), _member("1/3", "1/1")]))
    assert result.plans[0].members == ["1/1", "1/2", "1/10"]
