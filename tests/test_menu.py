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


def test_settings_update_session(env):
    tmp, cfg_path, inv, raw = env
    s = _session(env)
    # output dir, offline replay?, save raw?, manifest?, new switch name
    console = ScriptedConsole([str(tmp / "other"), "n", "n", "y", "new-99"])
    M.action_settings(s, console)
    assert s.output_dir == tmp / "other"
    assert s.offline_dir is None              # answered "no" -> live SSH
    assert s.write_manifest
    assert s.new_switch == "new-99"


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
