"""Interactive menu: session model, target selection, output actions."""

import shutil
from pathlib import Path

import pytest
from rich.console import Console

from switch_migrator import menu as M
from switch_migrator.config import Credentials, load_config
from switch_migrator.models import Platform

FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedConsole(Console):
    """A Console whose input() replays a scripted list of answers."""

    def __init__(self, answers):
        super().__init__(file=open("/dev/null", "w"), force_terminal=False)
        self._answers = list(answers)
        self.asked = []

    def input(self, prompt="", **kw):
        self.asked.append(str(prompt))
        if not self._answers:
            raise EOFError("script exhausted")
        return self._answers.pop(0)


@pytest.fixture
def env(tmp_path):
    """A config + inventory + offline capture dir the menu can work on."""
    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copytree(FIXTURES / "voss_prod" / "gx-11-s72-p1-priv", raw / "sw-voss")
    shutil.copytree(FIXTURES / "ers", raw / "sw-ers")
    (raw / "sw-voss" / "show_mac_address_table.txt").write_text(
        "VLAN  STATUS   MAC-ADDRESS       INTERFACE\n"
        "695   learned  00:11:22:00:00:01 Port-1/7\n")
    shutil.copy(FIXTURES / "voss" / "show_running_config.txt",
                raw / "sw-voss" / "show_running_config.txt")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text('excluded_vlan_names: ["quarant*"]\n')
    inv = tmp_path / "inv.yaml"
    inv.write_text("switches:\n"
                   "  - name: sw-voss\n    platform: voss\n"
                   "  - name: sw-ers\n    platform: ers\n")
    return tmp_path, cfg_path, inv, raw


def _session(env):
    tmp, cfg_path, inv, raw = env
    from switch_migrator.config import load_inventory
    s = M.Session(config_path=cfg_path,
                  cfg=load_config(cfg_path, require_fabric=False),
                  inventory_path=inv, output_dir=tmp / "out", offline_dir=raw)
    s.all_targets = load_inventory(inv)
    s.selected = list(s.all_targets)
    return s


def _offline_creds(s, console):
    return Credentials("offline", "offline"), Credentials("offline", "offline")


# ------------------------------ session ------------------------------------

def test_menu_opens_without_fabric_config(env, monkeypatch):
    # a config with no dvr_controllers/isid_conventions must not block the menu
    tmp, cfg_path, inv, raw = env
    console = ScriptedConsole(["0"])          # straight to quit
    rc = M.run_menu(cfg_path, inv, tmp / "out", console=console,
                    creds_fn=_offline_creds)
    assert rc == 0


def test_to_args_matches_cli_namespace_shape(env):
    s = _session(env)
    args = s.to_args(migration_sheets=True)
    # the menu reuses the CLI's collection path, so the namespace must carry
    # every attribute that path reads
    for attr in ("offline", "save_raw", "output_dir", "extract_config",
                 "migration_sheets", "no_fabric", "new_switch"):
        assert hasattr(args, attr), attr
    assert args.migration_sheets is True


# ------------------------------ selection ----------------------------------

def test_select_subset_by_number(env):
    s = _session(env)
    console = ScriptedConsole(["2"])          # keep only the second switch
    M.action_select(s, console)
    assert [t.name for t in s.selected] == ["sw-ers"]


def test_select_range_and_all(env):
    s = _session(env)
    M.action_select(s, ScriptedConsole(["1-2"]))
    assert len(s.selected) == 2
    M.action_select(s, ScriptedConsole(["all"]))
    assert len(s.selected) == 2


def test_select_ignores_bad_input_without_crashing(env):
    s = _session(env)
    M.action_select(s, ScriptedConsole(["9,abc"]))
    assert len(s.selected) == 2               # unchanged, nothing valid picked


# ------------------------------ collect + outputs --------------------------

def test_collect_once_then_multiple_outputs(env):
    s = _session(env)
    # collect: (no fabric question - config has no DvRs), config? n, macs? y
    M.action_collect(s, ScriptedConsole(["n", "y"]), _offline_creds)
    assert s.has_data and len(s.audits) == 2
    # the MAC/migration-sheet collection reads the running-config for its
    # tagging, but declining the config extract means the text is not kept -
    # so the session must not claim to have it
    assert s.collected_macs and not s.collected_config
    collected_at = s.collected_at

    # two different outputs from the SAME collected data - no re-collection
    M.action_inventory(s, ScriptedConsole([]))
    M.action_sheets(s, ScriptedConsole(["new-01"]))
    assert s.collected_at == collected_at

    out = s.output_dir
    assert list(out.glob("inventory-report-*.xlsx"))
    assert list(out.glob("migration-sheets-*.xlsx"))
    assert list(out.glob("migration-commands-*.txt"))


def test_audit_refuses_without_fabric_data(env):
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    assert not s.collected_fabric
    M.action_audit(s, ScriptedConsole([]))
    # nothing written: the audit needs fabric data it doesn't have
    assert not list(s.output_dir.glob("migration-audit-*.xlsx"))


def test_config_extract_refuses_without_running_config(env):
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    M.action_config(s, ScriptedConsole([]))
    assert not (s.output_dir / "config").exists()


def test_config_extract_writes_when_running_config_present(env):
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["y", "n"]), _offline_creds)  # config yes
    assert s.collected_config
    M.action_config(s, ScriptedConsole([]))
    assert (s.output_dir / "config" / "sw-voss.cfg").is_file()


def test_settings_change_one_at_a_time(env):
    """The settings menu changes exactly the setting that was picked - a
    crash (or an abort) mid-way can no longer lose every other answer."""
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    console = ScriptedConsole([
        "1", str(tmp / "other"),                        # output dir
        "4", "y",                                       # manifest on
        "6", "y", "Frankfurt=gx-11,gx-12; Munich=mu-01",  # split + groups
        "8", "new-99",                                  # new switch
        "",                                             # back to the menu
    ])
    M.action_settings(s, console)
    assert s.output_dir == tmp / "other"
    assert s.offline_dir == raw               # untouched: still the replay dir
    assert s.write_manifest
    assert s.split_by_location
    assert s.location_groups == {"Frankfurt": ["gx-11", "gx-12"],
                                 "Munich": ["mu-01"]}
    assert s.new_switch == "new-99"


def test_settings_keeps_the_config_groups_when_none_are_typed(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_settings(s, ScriptedConsole(["6", "y", "", ""]))
    assert s.split_by_location and s.location_groups == {}


def test_settings_rejects_a_mistyped_group_rather_than_scattering_ports(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_settings(s, ScriptedConsole(["6", "y", "Frankfurt gx-11", ""]))
    assert s.location_groups == {}      # fell back to the config, not a half-group


def test_x_aborts_an_action_at_any_prompt(env):
    import pytest as _pytest
    s = _session(env)
    with _pytest.raises(M.Abort):
        M.action_settings(s, ScriptedConsole(["1", "x"]))
    with _pytest.raises(M.Abort):
        M.action_generate_mlt(s, ScriptedConsole(["x"]))


def test_blank_at_the_main_menu_redisplays_instead_of_quitting(env):
    tmp, cfg_path, inv, raw = env
    console = ScriptedConsole(["", "", "0"])   # two stray Enters, then quit
    rc = M.run_menu(cfg_path, inv, tmp / "out", console=console,
                    creds_fn=_offline_creds)
    assert rc == 0
    # all three answers were consumed: blank never quit the menu early
    assert not console._answers


# ------------------------- snapshot & dry run -------------------------------

def test_snapshot_save_then_load_restores_the_session(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "y"]), _offline_creds)
    before = [(a.name, len(a.ports), len(a.mlts)) for a in s.audits]
    assert before

    snap = tmp / "snap.json"
    M.action_snapshot(s, ScriptedConsole(["1", str(snap)]))
    assert snap.is_file()

    fresh = _session(env)
    assert not fresh.has_data
    M.action_snapshot(fresh, ScriptedConsole([str(snap)]))
    assert [(a.name, len(a.ports), len(a.mlts)) for a in fresh.audits] == before
    assert fresh.loaded_from == snap
    assert fresh.collected_macs                 # MAC collection is remembered


def test_reports_from_a_loaded_snapshot_need_no_devices(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "y"]), _offline_creds)
    snap = tmp / "snap.json"
    M.action_snapshot(s, ScriptedConsole(["1", str(snap)]))

    fresh = _session(env)
    fresh.offline_dir = None                    # no replay dir, no SSH, nothing
    M.action_snapshot(fresh, ScriptedConsole([str(snap)]))
    fresh.new_switch = "new-01"
    M.action_sheets(fresh, ScriptedConsole([]))
    assert list((tmp / "out").glob("migration-sheets-*.xlsx"))


def test_loading_a_bad_snapshot_keeps_the_session_intact(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    kept = list(s.audits)
    bad = tmp / "bad.json"
    bad.write_text("{}")
    M.action_snapshot(s, ScriptedConsole(["2", str(bad)]))
    assert s.audits == kept and s.loaded_from is None


def test_dry_run_action_sends_nothing(env):
    s = _session(env)
    console = ScriptedConsole(["n", "y"])       # no running-config, yes MACs
    M.action_dry_run(s, console)
    assert not s.has_data                       # a preview is not a collection


def test_manifest_is_written_when_enabled(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    s.write_manifest = True
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    manifests = list((tmp / "out").glob("manifest-*.json"))
    assert len(manifests) == 1
    import json
    data = json.loads(manifests[0].read_text())
    assert data["totals"]["switches"] == 2
    assert data["switches"][0]["commands"]["sent"] > 0


# ------------------- health / MLT blocks / verification ---------------------

def test_health_action_writes_a_report_and_needs_no_devices(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    s.offline_dir = None                        # nothing left to talk to
    M.action_health(s, ScriptedConsole([]))
    assert list((tmp / "out").glob("health-check-*.xlsx"))


def test_generate_mlt_action_writes_blocks(env, tmp_path):
    import csv
    tmp, cfg_path, inv, raw = env
    sheet = tmp / "cabling.csv"
    with sheet.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Old switch", "Old port", "NEW switch", "NEW port",
                    "MLT ID", "MLT name", "Type"])
        w.writerow(["gx-01", "1/1", "leaf-01", "1/1", "35", "srv-lag", "mlt"])
        w.writerow(["gx-01", "1/2", "leaf-01", "1/2", "35", "srv-lag", "mlt"])
    s = _session(env)
    M.action_generate_mlt(s, ScriptedConsole([str(sheet), "y"]))
    out = (tmp / "out" / "config" / "mlt-blocks.cfg").read_text()
    assert 'mlt 35 enable name "srv-lag"' in out
    assert "mlt 35 member 1/1,1/2" in out
    assert "smlt" in out


def test_generate_mlt_action_reports_a_bad_sheet(env):
    tmp, cfg_path, inv, raw = env
    bad = tmp / "notasheet.csv"
    bad.write_text("a,b\n1,2\n")
    s = _session(env)
    M.action_generate_mlt(s, ScriptedConsole([str(bad)]))
    assert not (tmp / "out" / "config" / "mlt-blocks.cfg").exists()


def test_verify_action_collects_the_switches_the_sheet_names(env):
    import csv
    tmp, cfg_path, inv, raw = env
    sheet = tmp / "cabling.csv"
    with sheet.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Old switch", "Old port", "NEW switch", "NEW port",
                    "Port ID"])
        # 'sw-voss' is the offline capture, so it stands in for the new switch
        w.writerow(["old-01", "1/7", "sw-voss", "1/1", "P0001"])
    s = _session(env)
    M.action_verify(s, ScriptedConsole([str(sheet)]), _offline_creds)
    assert list((tmp / "out").glob("verification-*.xlsx"))


# ------------------- auto-snapshot, pre-flight, profiles ---------------------

def test_collect_auto_saves_a_snapshot(env):
    """Crash insurance: the expensive collection lands on disk immediately,
    timestamped, without being asked for."""
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    snaps = list((tmp / "out" / "snapshots").glob("snapshot-*.json"))
    assert len(snaps) == 1


def test_snapshot_can_be_loaded_by_number(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    M.action_collect(s, ScriptedConsole(["n", "y"]), _offline_creds)
    fresh = _session(env)
    assert not fresh.has_data
    # option 8 lists the auto-saved snapshot; '1' picks the newest
    M.action_snapshot(fresh, ScriptedConsole(["1"]))
    assert fresh.has_data
    assert fresh.collected_macs


def test_auto_snapshot_can_be_turned_off(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    s.auto_snapshot = False
    M.action_collect(s, ScriptedConsole(["n", "n"]), _offline_creds)
    assert not (tmp / "out" / "snapshots").exists()


def test_preflight_offline_session_skips_the_check(env, monkeypatch):
    from switch_migrator import connection as C
    s = _session(env)                              # offline_dir is set

    def boom(*a, **k):
        raise AssertionError("must not be called offline")

    monkeypatch.setattr(C, "quick_auth_check", boom)
    creds = Credentials("u", "p")
    assert M._preflight_credentials(s, ScriptedConsole([]), creds) is creds


def test_preflight_lets_wrong_credentials_be_corrected(env, monkeypatch):
    from switch_migrator import connection as C
    s = _session(env)
    s.offline_dir = None                           # live SSH session
    results = iter(["auth", None])                 # refused, then accepted
    seen = []

    def fake_check(host, creds, ssh):
        seen.append((host, creds.username, creds.password))
        return next(results)

    monkeypatch.setattr(C, "quick_auth_check", fake_check)
    console = ScriptedConsole(["admin2", "pw2"])   # corrected username + password
    out = M._preflight_credentials(s, console, Credentials("admin", "pw"))
    assert out == Credentials("admin2", "pw2")
    assert seen[0][1] == "admin" and seen[1][1] == "admin2"


def test_preflight_unreachable_is_a_warning_not_a_verdict(env, monkeypatch):
    from switch_migrator import connection as C
    s = _session(env)
    s.offline_dir = None
    monkeypatch.setattr(C, "quick_auth_check",
                        lambda *a, **k: "SSHException: timed out")
    creds = Credentials("admin", "pw")
    # continue? -> yes
    assert M._preflight_credentials(s, ScriptedConsole(["y"]), creds) is creds
    # continue? -> no
    assert M._preflight_credentials(s, ScriptedConsole(["n"]), creds) is None


def test_profiles_save_then_load_into_a_fresh_session(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    s.profiles_path = tmp / "profiles.yaml"
    s.new_switch = "leaf-a-01"
    s.save_raw = True
    M.action_profiles(s, ScriptedConsole(["2", "site-a"]))
    assert (tmp / "profiles.yaml").is_file()

    fresh = _session(env)
    fresh.profiles_path = tmp / "profiles.yaml"
    assert fresh.new_switch == ""
    M.action_profiles(fresh, ScriptedConsole(["1", "site-a"]))
    assert fresh.new_switch == "leaf-a-01"
    assert fresh.save_raw
    # loading only SET values - the session stays fully changeable
    fresh.new_switch = "changed-later"
    assert fresh.new_switch == "changed-later"


# ------------------------------ getting out again --------------------------

class _InterruptingConsole(ScriptedConsole):
    """A console whose input() raises KeyboardInterrupt, like a real Ctrl-C."""

    def __init__(self, times=3):
        super().__init__([])
        self.times = times
        self.count = 0

    def input(self, prompt="", **kw):
        self.count += 1
        if self.count <= self.times:
            raise KeyboardInterrupt
        raise EOFError("script exhausted")


def test_ctrl_c_at_the_main_menu_quits(env):
    """Ctrl-C at the menu prompt used to be swallowed and the loop continued,
    so there was no way out of the program short of SIGKILL."""
    tmp, cfg_path, inv, raw = env
    console = _InterruptingConsole()
    rc = M.run_menu(cfg_path, inv, tmp / "out", console=console,
                    creds_fn=_offline_creds)
    assert rc == 0
    assert console.count == 1, "it should quit on the first Ctrl-C, not loop"


def test_x_at_the_main_menu_still_just_reshows_it(env):
    """'x' abandons an action; at the menu there is no action, so it redraws -
    only Ctrl-C means quit."""
    tmp, cfg_path, inv, raw = env
    console = ScriptedConsole(["x", "0"])
    assert M.run_menu(cfg_path, inv, tmp / "out", console=console,
                      creds_fn=_offline_creds) == 0
    assert len(console.asked) == 2


def test_ctrl_c_inside_an_action_returns_to_the_menu(env):
    """Ctrl-C at a prompt WITHIN an action keeps its documented meaning."""
    s = _session(env)

    class _OneInterrupt(ScriptedConsole):
        def input(self, prompt="", **kw):
            self.asked.append(str(prompt))
            raise KeyboardInterrupt

    with pytest.raises(M.Abort):
        M._ask(_OneInterrupt([]), "Choose")


# ------------------------------ profiles ------------------------------------

def test_menu_applies_a_named_profile(env, tmp_path):
    """`--menu --profile X` used to ignore the profile entirely."""
    tmp, cfg_path, inv, raw = env
    profiles = tmp / "profiles.yaml"
    profiles.write_text(
        "profiles:\n"
        "  site-a:\n"
        f"    output_dir: {tmp / 'site-a-out'}\n"
        "    new_switch: leaf-a-01\n"
        "    split_by_location: true\n")

    seen = {}

    def _capture(s, console):
        seen["output_dir"] = s.output_dir
        seen["new_switch"] = s.new_switch
        seen["split"] = s.split_by_location
        raise M.Abort()

    import switch_migrator.menu as menu_mod
    original = menu_mod.action_select
    menu_mod.action_select = _capture
    try:
        M.run_menu(cfg_path, inv, tmp / "out", console=ScriptedConsole(["1", "0"]),
                   creds_fn=_offline_creds, profile="site-a",
                   profiles_file=profiles)
    finally:
        menu_mod.action_select = original

    assert seen["output_dir"] == tmp / "site-a-out"
    assert seen["new_switch"] == "leaf-a-01"
    assert seen["split"] is True


class _RecordingConsole(ScriptedConsole):
    """A scripted console that also keeps what was printed."""

    def __init__(self, answers):
        Console.__init__(self, record=True, width=200,
                         file=open("/dev/null", "w"), force_terminal=False)
        self._answers = list(answers)
        self.asked = []


def test_menu_says_so_when_the_named_profile_is_missing(env):
    tmp, cfg_path, inv, raw = env
    console = _RecordingConsole(["0"])
    rc = M.run_menu(cfg_path, inv, tmp / "out", console=console,
                    creds_fn=_offline_creds, profile="nope",
                    profiles_file=tmp / "profiles.yaml")
    assert rc == 0
    assert "No profile 'nope'" in console.export_text()
