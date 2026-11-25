"""The MAC check by migration-check (switch_migrator/mac_check.py and its wiring).

The end-to-end tests go through `cli.main` and the real OfflineRunner: a baseline of an
old ERS switch with --mac-snapshot, then --verify-migration --mac-baseline against a
new Fabric Engine switch. The BOSS baseline is this repo's own ers fixtures; the MAC
tables are written here.
"""

import argparse
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from migration_check.platforms.boss import COMMANDS, LLDP, MAC_TABLE
from switch_migrator import cli, mac_check, menu
from switch_migrator.cabling_sheet import CablingRow, Sheet
from switch_migrator.collectors.switch import Reads
from switch_migrator.config import (
    ConfigError,
    Credentials,
    MacCheckSettings,
    SwitchTarget,
    load_config,
)
from switch_migrator.connection import BaseRunner, CommandError, OfflineRunner
from switch_migrator.models import Platform
from switch_migrator.workflow.collect import CollectOptions, preview_commands
from switch_migrator.workflow.macs import mac_check_targets

FIXTURES = Path(__file__).parent / "fixtures"

BOSS_MACS = """\
Mac Address Table Aging Time: 300
Number of addresses: 4

   MAC Address    Vid   Type       Source
----------------- ---- ------- --------------
0011.2200.0011       100 Dynamic Port:1
0011.2200.0012       100 Dynamic Port:2
0011.2200.0013       200 Dynamic Port:11
0011.2200.0099       100 Dynamic Trunk:1
"""

# 'Vlan Fdb' shape as Fabric Engine 9.4 prints it (fabric-weft lab captures)
FE_MACS = """\
=======================================================================================================
                                               Vlan Fdb
=======================================================================================================
VLAN            MAC                                   SMLT
ID   STATUS     ADDRESS            INTERFACE          REMOTE   TUNNEL
-------------------------------------------------------------------------------------------------------
100  learned    00:11:22:00:00:11  Port-1/2           false    -
100  learned    00:11:22:00:00:12  Port-1/1           false    -

2 out of 2 entries in all fdb(s) displayed.
"""

ERS_FILES = ("show_interfaces", "show_lldp_neighbor", "show_mlt", "show_vlan")


SWAPPED = 2               # the two cables the scenario swaps
ROUNDS_TO_SETTLE = 2      # round 1, then a round 2 that finds nothing new


def _sheet(*rows: tuple[str, str, str, str]) -> Sheet:
    return Sheet(rows=[CablingRow(old_switch=a, old_port=b, new_switch=c, new_port=d)
                       for a, b, c, d in rows])


def _old_ers_raw(root: Path, name: str = "old-1") -> Path:
    device = root / name
    device.mkdir(parents=True)
    for stem in ERS_FILES:
        shutil.copy(FIXTURES / "ers" / f"{stem}.txt", device / f"{stem}.txt")
    (device / "show_mac_address_table.txt").write_text(BOSS_MACS)
    return root


def _new_fe_raw(root: Path, name: str = "leaf-1") -> Path:
    device = root / name
    device.mkdir(parents=True)
    (device / "show_vlan_mac_address_entry.txt").write_text(FE_MACS)
    for stem in ("show_lldp_neighbor", "show_mlt", "show_vlan_basic"):
        (device / f"{stem}.txt").write_text("")
    return root  # every other command has no capture file: no answer


def _sheet_csv(path: Path) -> Path:
    path.write_text("Old switch,Old port,NEW switch,NEW port\n"
                    "old-1,1,leaf-1,1/1\n"
                    "old-1,2,leaf-1,1/2\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# small pieces
# --------------------------------------------------------------------------

def test_port_map_takes_migrated_rows_only():
    sheet = _sheet(("old-1", "1", "leaf-1", "1/1"), ("old-1", "5", "", ""))
    assert mac_check.port_map(sheet) == {("old-1", "1"): ("leaf-1", "1/1")}


class _Scripted(BaseRunner):
    """Answers from a dict; a value starting with '!' is a lost answer (transport)."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.command_log = []
        self.sent: list[str] = []

    def run(self, command):
        self.sent.append(command)
        text = self.answers.get(command, "")
        if text.startswith("!"):
            raise CommandError(command, "(transport TimeoutError) read timed out")
        if text.startswith("%"):
            raise CommandError(command, text)
        return text


def test_the_memo_keeps_answers_and_rejections_but_not_lost_commands():
    runner = _Scripted({"show a": "out", "show b": "% Invalid input detected",
                        "show c": "!lost"})
    memo = mac_check.AnswerMemo(runner)
    assert memo.run("show a") == "out"
    for command in ("show b", "show c"):
        with pytest.raises(CommandError):
            memo.run(command)
    assert memo.answers == {"show a": "out", "show b": "% Invalid input detected"}


def test_the_recorder_sends_only_what_the_collection_did_not(tmp_path):

    runner = _Scripted({c: f"text of {c}" for c in COMMANDS})
    memo = mac_check.AnswerMemo(runner)
    for command in COMMANDS[:3]:
        memo.run(command)                    # what the collection already asked
    recorder = mac_check.SnapshotRecorder(tmp_path / "snap", "pre")
    recorder.record(SwitchTarget("old-1", "10.0.0.1", Platform.ERS), runner, memo)
    recorder.finish()

    assert runner.sent == list(COMMANDS)     # each command exactly once
    meta = json.loads((tmp_path / "snap" / "snapshot.json").read_text())
    assert {c: e["status"] for c, e in meta["devices"]["old-1"]["commands"].items()} \
        == dict.fromkeys(COMMANDS, "ok")


def test_a_rejected_command_is_recorded_as_rejected_a_lost_one_as_failed(tmp_path):

    runner = _Scripted({MAC_TABLE: "% Invalid input detected", LLDP: "!lost"})
    recorder = mac_check.SnapshotRecorder(tmp_path / "snap", "pre")
    recorder.record(SwitchTarget("old-1", "old-1", Platform.ERS), runner)
    recorder.finish()
    commands = json.loads((tmp_path / "snap" / "snapshot.json").read_text())[
        "devices"]["old-1"]["commands"]
    assert commands[MAC_TABLE]["status"] == "rejected"
    assert commands[LLDP]["status"] == "failed"


def test_an_unreachable_switch_is_in_the_snapshot_with_every_command_failed(tmp_path):
    recorder = mac_check.SnapshotRecorder(tmp_path / "snap", "after", only={"leaf-1"})
    recorder.unreachable(SwitchTarget("leaf-1", "leaf-1", Platform.VOSS), "timed out")
    recorder.unreachable(SwitchTarget("old-1", "old-1", Platform.ERS), "ignored")
    recorder.finish()
    devices = json.loads((tmp_path / "snap" / "snapshot.json").read_text())["devices"]
    assert list(devices) == ["leaf-1"]
    assert {e["status"] for e in devices["leaf-1"]["commands"].values()} == {"failed"}


def test_old_voss_raw_folders_are_named_as_unreadable(tmp_path):
    voss = tmp_path / "raw" / "old-voss"
    voss.mkdir(parents=True)
    (voss / "show_interfaces_gigabitethernet_fdb_entry.txt").write_text("")
    _old_ers_raw(tmp_path / "raw", "old-ers")
    assert mac_check.unreadable_baselines([tmp_path / "raw"]) == [
        f"{tmp_path / 'raw'}/old-voss"]


def test_every_command_migration_check_asks_for_is_a_show():
    for platform in Platform:
        assert all(c.startswith("show ") for c in mac_check.commands_for(platform))


def test_the_watch_settings_come_from_the_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mac_check: {interval: 30, rounds: 4, stable: 2}\n")
    assert load_config(path, require_fabric=False).mac_check == MacCheckSettings(30.0, 4, 2)
    path.write_text("mac_check: {rounds: 0}\n")
    with pytest.raises(ConfigError, match="rounds >= 1"):
        load_config(path, require_fabric=False)


# --------------------------------------------------------------------------
# argument checks
# --------------------------------------------------------------------------

def _args(**overrides) -> argparse.Namespace:
    values = {"verify_migration": Path("sheet.csv"), "mac_baseline": [],
              "mac_snapshot": None, "mac_watch": False, "generate_mlt": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_mac_arguments_are_checked_before_any_switch(tmp_path):
    check = cli._check_mac_args
    with pytest.raises(ConfigError, match="goes with --verify-migration"):
        check(_args(verify_migration=None, mac_baseline=[tmp_path]), None)
    with pytest.raises(ConfigError, match="saved snapshot"):
        check(_args(mac_baseline=[tmp_path]), tmp_path / "s.json")
    with pytest.raises(ConfigError, match="no such folder"):
        check(_args(mac_baseline=[tmp_path / "nope"]), None)
    with pytest.raises(ConfigError, match="goes with --mac-baseline"):
        check(_args(mac_watch=True), None)
    with pytest.raises(ConfigError, match="saves the new switches' snapshot"):
        check(_args(mac_snapshot=tmp_path / "new"), None)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("")
    with pytest.raises(ConfigError, match="never overwritten"):
        check(_args(verify_migration=None, mac_snapshot=tmp_path / "full"), None)
    check(_args(mac_baseline=[tmp_path]), None)
    check(_args(verify_migration=None, mac_snapshot=tmp_path / "new"), None)


def test_only_the_switches_links_moved_onto_are_read():
    sheet = _sheet(("old-1", "1", "leaf-1", "1/1"))
    old = SwitchTarget(name="old-1", host="old-1", platform=Platform.ERS)
    new = SwitchTarget(name="leaf-1", host="leaf-1", platform=Platform.VOSS)
    assert mac_check_targets([old, new], sheet) == [new]


def test_the_dry_run_lists_the_mac_commands_once(tmp_path):
    cfg = load_config(_empty_config(tmp_path), require_fabric=False)
    target = SwitchTarget("leaf-1", "leaf-1", Platform.VOSS)
    opts = CollectOptions(reads=Reads.for_run(migration_sheets=True))
    plain = preview_commands([target], cfg, opts)["leaf-1"]
    with_macs = preview_commands([target], cfg, opts, mac_for={"leaf-1"})["leaf-1"]
    added = with_macs[len(plain):]
    assert with_macs[:len(plain)] == plain
    assert "show vlan mac-address-entry" in added
    assert not set(added) & set(plain)       # nothing the collection sends is repeated
    assert "show mlt" not in added           # e.g. this one is reused


# --------------------------------------------------------------------------
# end to end through main()
# --------------------------------------------------------------------------

def _empty_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("mac_check: {interval: 0, rounds: 3, stable: 1}\n")
    return path


def test_baseline_then_verification_end_to_end(tmp_path):
    config = _empty_config(tmp_path)
    old_raw = _old_ers_raw(tmp_path / "old-raw")
    baseline = tmp_path / "baseline"

    code = cli.main(["-c", str(config), "-s", "old-1:ers", "--no-fabric",
                     "--migration-sheets", "--offline", str(old_raw),
                     "--mac-snapshot", str(baseline), "--no-excel",
                     "-o", str(tmp_path / "out-pre")])
    assert code in (0, 1)                    # 1: the fixture's dead MLT is a finding
    meta = json.loads((baseline / "snapshot.json").read_text())
    assert meta["devices"]["old-1"]["platform"] == "boss"
    assert {e["status"] for e in meta["devices"]["old-1"]["commands"].values()} == {"ok"}

    out = tmp_path / "out-post"
    new_raw = _new_fe_raw(tmp_path / "new-raw")
    code = cli.main(["-c", str(config), "--no-fabric", "--no-excel",
                     "--verify-migration", str(_sheet_csv(tmp_path / "sheet.csv")),
                     "--mac-baseline", str(baseline), "--offline", str(new_raw),
                     "-o", str(out)])
    assert code == 1                         # the verification itself fails links
    report = json.loads(next(out.glob("mac-check-*.json")).read_text())
    assert report["summary"]["other-port"] == SWAPPED
    assert report["summary"]["missing"] == 1
    after = next(out.glob("mac-check-*/after"))
    assert json.loads((after / "snapshot.json").read_text())["devices"].keys() == {"leaf-1"}


def test_the_watch_rereads_until_nothing_changes(tmp_path):
    baseline = tmp_path / "baseline"
    recorder = mac_check.SnapshotRecorder(baseline, "pre")
    old_raw = _old_ers_raw(tmp_path / "old-raw")
    recorder.record(SwitchTarget("old-1", "old-1", Platform.ERS),
                    OfflineRunner("old-1", old_raw))
    recorder.finish()

    new_raw = _new_fe_raw(tmp_path / "new-raw")
    leaf = SwitchTarget("leaf-1", "leaf-1", Platform.VOSS)
    out = tmp_path / "out"
    first = mac_check.after_snapshot_path(out, "stamp", watching=True)
    round_one = mac_check.SnapshotRecorder(first, "after")
    round_one.record(leaf, OfflineRunner("leaf-1", new_raw))
    round_one.finish()

    lines: list[str] = []
    result = mac_check.check(
        mac_check.MacCheckJob([baseline], first, _sheet(("old-1", "1", "leaf-1", "1/1")),
                              out, "stamp"),
        mac_check.Watching(MacCheckSettings(0, 5, 1), [leaf],
                           lambda t: OfflineRunner(t.name, new_raw), on_round=lines.append))

    assert len(lines) == ROUNDS_TO_SETTLE
    assert "the number not found stopped falling" in result.stopped
    assert result.snapshot.name == "round-02"
    assert result.text.read_text().startswith(lines[0])


def test_ctrl_c_ends_the_watch_not_the_run(tmp_path, monkeypatch):
    baseline = _old_ers_raw(tmp_path / "old-raw")
    new_raw = _new_fe_raw(tmp_path / "new-raw")
    leaf = SwitchTarget("leaf-1", "leaf-1", Platform.VOSS)
    first = mac_check.after_snapshot_path(tmp_path / "out", "stamp", watching=True)
    recorder = mac_check.SnapshotRecorder(first, "after")
    recorder.record(leaf, OfflineRunner("leaf-1", new_raw))
    recorder.finish()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mac_check, "capture", interrupted)
    result = mac_check.check(
        mac_check.MacCheckJob([baseline], first, _sheet(), tmp_path / "out", "stamp"),
        mac_check.Watching(MacCheckSettings(0, 5, 3), [leaf],
                           lambda t: OfflineRunner(t.name, new_raw)))
    assert result.stopped == "stopped after round 1: interrupted"
    assert result.json.is_file()


# --------------------------------------------------------------------------
# the interactive menu
# --------------------------------------------------------------------------

class _ScriptedConsole:
    """Just enough of a rich Console for the menu: scripted answers, no output."""

    def __init__(self, answers):

        self._console = Console(file=io.StringIO(), force_terminal=False)
        self._answers = list(answers)
        self.asked: list[str] = []

    def input(self, prompt="", **kw):
        self.asked.append(str(prompt))
        if not self._answers:
            raise EOFError("script exhausted")
        return self._answers.pop(0)

    def __getattr__(self, name):
        return getattr(self._console, name)


def test_the_menu_baseline_is_offered_to_the_verification(tmp_path):

    raw = _old_ers_raw(tmp_path / "raw")
    _new_fe_raw(raw)                         # the new switch replays from the same folder
    config = _empty_config(tmp_path)
    s = menu.Session(config_path=config, cfg=load_config(config, require_fabric=False),
                     output_dir=tmp_path / "out", offline_dir=raw)
    s.all_targets = s.selected = [SwitchTarget("old-1", "old-1", Platform.ERS)]
    def creds(s, c):
        return Credentials("offline", "x"), Credentials("offline", "x")

    # running-config? no - MAC tables? yes - MAC snapshot? yes
    menu.action_collect(s, _ScriptedConsole(["n", "y", "y"]), creds)
    assert s.mac_snapshot is not None
    assert (s.mac_snapshot / "snapshot.json").is_file()

    console = _ScriptedConsole([str(_sheet_csv(tmp_path / "sheet.csv")), "", "n"])
    menu.action_verify(s, console, creds)    # blank baseline answer = the offered one
    assert any(str(s.mac_snapshot) in prompt for prompt in console.asked)
    report = json.loads(next((tmp_path / "out").glob("mac-check-*.json")).read_text())
    assert report["summary"]["other-port"] == SWAPPED
