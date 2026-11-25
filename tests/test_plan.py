"""The migration plan: every rule of the allocator, on synthetic old switches."""

import csv
import io
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook
from rich.console import Console

from switch_migrator import cabling_sheet, cli, menu, standard, verify
from switch_migrator import plan as P
from switch_migrator.config import Credentials, SwitchTarget, load_config
from switch_migrator.models import IstState, MltState, Platform, PortState, SwitchAudit
from switch_migrator.report.excel import write_excel
from switch_migrator.report.migration import assign_port_uids, build_cabling, build_plan_tables
from switch_migrator.report.migration_tables import build_verification

LAYOUT = P.Layout(ports=28, uplinks=4)          # 24 access ports, 25-28 uplinks


CONFIG_ERROR = 2          # main()'s exit code for a config error
ACCESS_PORTS = 24         # LAYOUT: 28 ports, the last 4 uplinks
MENU_PORTS = 28           # the port count typed into the menu
PROD_ISID_695 = 2500695   # the standard: prod prefix 250 + VLAN as 4 digits


def _port(port, usage="IN USE", mlt=None, neighbor="", uplink=False, **kw):
    return PortState(port=port, oper_up=usage != "UNCERTAIN", usage=usage,
                     mlt_id=mlt, lldp_neighbor=neighbor, is_uplink=uplink, **kw)


def _ers_stack():
    ports = [
        _port("1/1", neighbor="srv-a"), _port("1/2"), _port("1/3"),
        _port("1/4", usage="UNUSED"),
        _port("1/5", mlt=1), _port("2/5", mlt=1),                       # DMLT across units
        _port("1/6", mlt=2), _port("1/7", mlt=2), _port("2/6", mlt=2), _port("2/7", mlt=2),
        _port("1/8", usage="UNCERTAIN"),
        _port("1/25", neighbor="core-01", uplink=True),
        _port("2/1", neighbor="srv-a"),                                 # teamed? (R5)
    ]
    mlts = [MltState(1, "srv-lag", members=["1/5", "2/5"]),
            MltState(2, "big-lag", members=["1/6", "1/7", "2/6", "2/7"])]
    return SwitchAudit("ers-1", "ers-1", Platform.ERS, reachable=True, ports=ports, mlts=mlts)


def _plan(audits, switches=("leaf-a", "leaf-b"), layout=LAYOUT, **kw):
    return P.make_plan(audits, list(switches), layout, **kw)


def _where(plan):
    return {(m.link.old_switch, m.link.old_port): (m.new_switch, m.new_port, m.new_mlt_id)
            for m in plan.moves}


def test_the_layout_never_plans_the_uplink_ports():
    assert LAYOUT.access_ports == ACCESS_PORTS
    with pytest.raises(ValueError, match="more ports than uplinks"):
        P.Layout(ports=4, uplinks=4)


def test_dual_homed_from_the_bottom_single_homed_from_the_top():
    where = _where(_plan([_ers_stack()]))
    # a DMLT over two units: one leg per new peer, same port, MLT 4xx of that port
    assert where[("ers-1", "1/5")] == ("leaf-a", "1/1", 401)
    assert where[("ers-1", "2/5")] == ("leaf-b", "1/1", 401)
    # two links per side: consecutive ports, the MLT id is the FIRST port's (R3)
    assert where[("ers-1", "1/6")] == ("leaf-a", "1/2", 402)
    assert where[("ers-1", "1/7")] == ("leaf-a", "1/3", 402)
    assert where[("ers-1", "2/6")] == ("leaf-b", "1/2", 402)
    assert where[("ers-1", "2/7")] == ("leaf-b", "1/3", 402)
    # single-homed from the last access port down, balanced over the pair (R4)
    singles = [where[("ers-1", p)] for p in ("1/1", "1/2", "1/3", "1/8", "2/1")]
    assert singles == [("leaf-a", "1/24", None), ("leaf-b", "1/24", None),
                       ("leaf-a", "1/23", None), ("leaf-b", "1/23", None),
                       ("leaf-a", "1/22", None)]


def test_the_mlt_name_follows_the_template():
    plan = _plan([_ers_stack()])
    lag = standard.mlt_id_for_new_lag("1/1")
    assert {m.new_mlt_name for m in plan.moves if m.new_mlt_id == lag} == {"f11"}
    plan = _plan([_ers_stack()], mlt_name="srv-{id}")
    lag = standard.mlt_id_for_new_lag("1/2")
    assert {m.new_mlt_name for m in plan.moves if m.new_mlt_id == lag} == {f"srv-{lag}"}


def test_uplinks_and_unused_ports_do_not_move_and_say_why():
    reasons = {n.link.old_port: n.reason for n in _plan([_ers_stack()]).not_moved}
    assert reasons["1/25"].startswith("uplink to the core")
    assert "check at the rack" in reasons["1/4"]            # R7: marked, not planned


def test_uncertain_ports_are_planned_but_flagged():
    move = _plan([_ers_stack()]).move_for("ers-1", "1/8")
    assert move is not None
    assert "uncertain" in move.note


def test_an_old_smlt_across_a_vist_pair_is_one_attachment():
    def leaf(name, peer):
        ports = [_port("1/10", mlt=196), _port("1/8", mlt=1, neighbor=peer)]
        mlts = [MltState(196, "f110", admin="smlt", current="smlt", members=["1/10"]),
                MltState(1, "MLT001.svi", members=["1/8"], is_ist=True)]
        return SwitchAudit(name, name, Platform.VOSS, reachable=True, ports=ports,
                           mlts=mlts, ist=IstState(peer_ip="10.0.0.9"))
    plan = _plan([leaf("old-p1", "old-p2"), leaf("old-p2", "old-p1")])
    where = _where(plan)
    assert where[("old-p1", "1/10")] == ("leaf-a", "1/1", 401)
    assert where[("old-p2", "1/10")] == ("leaf-b", "1/1", 401)
    assert all("vIST" in n.reason for n in plan.not_moved)  # the peer link stays


def test_a_full_pair_overflows_into_the_next_and_then_reports():
    small = P.Layout(ports=3, uplinks=1)                     # 2 access ports
    plan = _plan([_ers_stack()], switches=["a1", "a2", "b1", "b2"], layout=small)
    used = {m.new_switch for m in plan.moves}
    assert used == {"a1", "a2", "b1", "b2"}
    assert plan.overflow
    assert any("did not fit" in p for p in plan.problems)
    for fill in plan.fill:
        assert fill.free >= 0


def test_pins_from_the_sheet_are_kept_and_the_rest_fills_around_them():
    pins = {("ers-1", "1/1"): ("leaf-b", "1/24", None, "")}
    plan = _plan([_ers_stack()], pins=pins)
    where = _where(plan)
    assert where[("ers-1", "1/1")] == ("leaf-b", "1/24", None)
    assert plan.move_for("ers-1", "1/1").rule == "pinned in the sheet"
    taken = [w[:2] for w in where.values()]
    assert len(taken) == len(set(taken))                     # nothing double-booked


def test_a_partly_pinned_aggregation_is_left_to_the_human():
    pins = {("ers-1", "1/5"): ("leaf-a", "1/5", 405, "f15")}
    plan = _plan([_ers_stack()], pins=pins)
    assert plan.move_for("ers-1", "2/5") is None
    assert any("place those by hand" in p for p in plan.problems)


def test_the_same_input_gives_the_same_plan():
    assert _where(_plan([_ers_stack()])) == _where(_plan([_ers_stack()]))


def test_a_server_on_two_units_with_the_same_name_is_a_hint_not_an_action():
    plan = _plan([_ers_stack()])
    assert any("'srv-a'" in h and "teamed" in h for h in plan.hints)
    assert _where(plan)[("ers-1", "2/1")][2] is None         # still a plain port


def test_an_unpaired_new_switch_is_named():
    plan = _plan([_ers_stack()], switches=["a1", "a2", "c1"])
    assert any("c1 has no peer" in p for p in plan.problems)


def test_conventions_copied_from_port_preparer():
    assert standard.mlt_id_for_new_lag("1/10") == standard.MLT_NEW_BASE + 10
    assert standard.mlt_name_for_new_lag("1/10") == "f110"
    mlt_id = standard.mlt_id_for_new_lag("1/10")
    assert standard.lacp_key_for(mlt_id) == mlt_id
    assert standard.isid_for_new_service(695, standard.Environment.PROD) == PROD_ISID_695
    assert standard.classify_isid(2510695).note.startswith("legacy variant")
    with pytest.raises(standard.ConventionError):
        standard.isid_for_new_service(31, standard.Environment.PROD)
    with pytest.raises(standard.ConventionError):
        standard.mlt_id_for_new_lag("1/1/2")


# --------------------------------------------------------------------------
# through the command line, on the fixture captures
# --------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _run(tmp_path, *extra):
    raw = tmp_path / "raw"
    if not raw.exists():
        shutil.copytree(FIXTURES / "ers", raw / "old-ers")
        shutil.copytree(FIXTURES / "voss", raw / "old-voss")
    config = tmp_path / "config.yaml"
    config.write_text("core_switch_patterns: ['dvr-*', 'core-*']\n"
                      "plan: {ports: 52, uplinks: 4}\n")
    out = tmp_path / f"out{len(list(tmp_path.glob('out*')))}"
    code = cli.main(["-c", str(config), "-s", "old-ers:ers", "-s", "old-voss:voss",
                     "--no-fabric", "--offline", str(raw), "--csv", "--no-excel",
                     "--plan", "--new-switch", "leaf-a,leaf-b", "-o", str(out), *extra])
    return code, next(out.glob("csv-*"))


def _rows(folder, name="cabling.csv"):
    with (folder / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_plan_prefills_the_cabling_sheet(tmp_path):
    code, csvs = _run(tmp_path)
    assert code in (0, 1)
    rows = {(r["Old switch"], r["Old port"]): r for r in _rows(csvs)}
    srv = rows[("old-voss", "2/1/1")]            # MLT 10, one link: a plain port
    # single-homed: from the top of the 48 access ports, never the 4 uplinks
    assert srv["NEW switch"] in ("leaf-a", "leaf-b")
    top_of_access = range(40, 48 + 1)
    assert int(srv["NEW port"].split("/")[1]) in top_of_access
    assert "single-homed" in srv["Plan note"]
    assert "one link" in srv["Plan note"]
    singles = [r for r in rows.values() if "single-homed" in r["Plan note"]]
    per_switch = [sum(r["NEW switch"] == s for r in singles) for s in ("leaf-a", "leaf-b")]
    assert abs(per_switch[0] - per_switch[1]) <= 1                # balanced (R4)
    uplink = rows[("old-ers", "49")]
    assert uplink["NEW port"] == ""
    assert uplink["Plan note"].startswith("not planned: uplink")
    assert {r["New switch"] for r in _rows(csvs, "plan.csv")} == {"leaf-a", "leaf-b"}
    assert _rows(csvs, "not_moved.csv")


def test_a_changed_port_is_kept_when_planning_again(tmp_path):
    _, csvs = _run(tmp_path)
    rows = _rows(csvs)
    for row in rows:
        if (row["Old switch"], row["Old port"]) == ("old-voss", "2/1/1"):
            row["NEW switch"], row["NEW port"] = "leaf-b", "1/30"
    edited = tmp_path / "edited.csv"
    with edited.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _, again = _run(tmp_path, "--pins", str(edited))
    moved = {(r["Old switch"], r["Old port"]): r for r in _rows(again)}
    assert (moved[("old-voss", "2/1/1")]["NEW switch"],
            moved[("old-voss", "2/1/1")]["NEW port"]) == ("leaf-b", "1/30")
    assert "pinned" in moved[("old-voss", "2/1/1")]["Plan note"]


def test_plan_needs_the_layout_and_the_new_switches(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    base = ["-c", str(config), "-s", "x:ers", "--no-fabric", "--plan",
            "-o", str(tmp_path / "out")]
    assert cli.main([*base, "--new-switch", "a,b"]) == CONFIG_ERROR          # no layout
    assert cli.main([*base, "--new-ports", "52",
                     "--new-uplinks", "4"]) == CONFIG_ERROR  # no switches
    assert cli.main([*base, "--new-switch", "a,b", "--new-ports", "4",
                     "--new-uplinks", "4"]) == CONFIG_ERROR      # nothing left to plan


def test_a_planned_row_is_pending_until_done_is_ticked():
    rows = [cabling_sheet.CablingRow(old_switch="o", old_port="1", new_switch="n",
                                     new_port="1/1", done=True),
            cabling_sheet.CablingRow(old_switch="o", old_port="2", new_switch="n",
                                     new_port="1/2")]
    sheet = cabling_sheet.Sheet(rows=rows)
    report = verify.verify(sheet, [])
    by_port = {v.old_port: v for v in report.verdicts}
    assert by_port["2"].result == verify.PENDING
    assert by_port["2"].reasons == ["planned, not ticked Done yet"]
    assert by_port["1"].result == verify.FAIL                 # moved, switch not read
    rows[0].done = None                                        # nobody ticked anything:
    assert {v.result for v in verify.verify(cabling_sheet.Sheet(rows=rows), [])
            .verdicts} == {verify.FAIL}                        # read as before


def test_the_menu_plans_with_the_layout_it_asks_for(tmp_path):


    class Scripted(Console):
        def __init__(self, answers):
            super().__init__(file=io.StringIO())
            self.answers = list(answers)

        def input(self, prompt="", **kw):
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "ers", raw / "old-ers")
    config = tmp_path / "config.yaml"
    config.write_text("core_switch_patterns: ['dvr-*']\n")
    s = menu.Session(config_path=config, cfg=load_config(config, require_fabric=False),
                     output_dir=tmp_path / "out", offline_dir=raw)
    s.all_targets = s.selected = [SwitchTarget("old-ers", "old-ers", Platform.ERS)]
    def creds(s, c):
        return Credentials("o", "o"), Credentials("o", "o")
    menu.action_collect(s, Scripted(["n", "y", "n"]), creds)
    # new switches, plan? yes, 28 ports, 4 uplinks, no pins
    menu.action_sheets(s, Scripted(["leaf-a,leaf-b", "y", "28", "4", ""]))
    wb = load_workbook(next((tmp_path / "out").glob("migration-sheets-*.xlsx")))
    assert {"Plan", "Not moved", "Plan notes"} <= set(wb.sheetnames)
    sheet = wb["Cabling"]
    header = [c.value for c in sheet[1]]
    ports = {r[header.index("NEW port")] for r in sheet.iter_rows(min_row=2, values_only=True)}
    assert "1/24" in ports
    assert s.cfg.plan.ports == MENU_PORTS


def test_a_planned_workbook_reads_back_only_its_cabling_rows(tmp_path):
    """'Not moved' and 'Verification' name old ports too; only Cabling counts."""

    audit = _ers_stack()
    assign_port_uids([audit])
    plan = _plan([audit])
    path = tmp_path / "planned.xlsx"
    write_excel([build_cabling([audit], ["leaf-a", "leaf-b"], plan),
                 *build_plan_tables(plan),
                 build_verification(verify.VerifyReport())], path)
    sheet = cabling_sheet.load(path)
    keys = [(r.old_switch, r.old_port) for r in sheet.rows]
    assert len(keys) == len(set(keys))
    assert sheet.sheets_read == ["Cabling"]


def test_port_ids_from_the_pinned_sheet_survive_a_new_switch():
    """Council review: a switch added to the inventory shifted every later
    Port ID, including IDs already on a printed rack sheet."""

    first = _ers_stack()
    assign_port_uids([first])
    printed = {("ers-1", p.port): p.uid for p in first.ports}

    earlier = SwitchAudit("aaa-new", "aaa-new", Platform.ERS, reachable=True,
                          ports=[_port("1/1"), _port("1/2")])     # sorts first
    again = _ers_stack()
    assign_port_uids([earlier, again], keep=printed)
    assert {("ers-1", p.port): p.uid for p in again.ports} == printed
    top = max(int(uid[1:]) for uid in printed.values())
    assert [p.uid for p in earlier.ports] == [f"P{top + 1:04d}", f"P{top + 2:04d}"]
