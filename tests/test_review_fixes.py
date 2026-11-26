"""Regressions from the feature review (2026-11-26).

Three things that were wrong, each with the failure it caused:

* `switch-migrator collect switches.yaml` wrote the collected state over the
  inventory file. `--save-snapshot` takes an optional value, so the verb's
  expansion put it last and the next word became the snapshot's file name.
* a `commands:` override holding a newline or ';' got past the read-only check,
  which only looks at the start of the value - the rest was sent to the device
  unchecked.
* a .csv the sheet came back as from a European Excel is semicolon-separated;
  it was rejected with an error blaming the header row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from switch_migrator import cabling_sheet, cli, paths
from switch_migrator import snapshot as snapshot_mod
from switch_migrator.config import ConfigError, load_config
from switch_migrator.models import FabricState

CONFIG_ERROR = 2

_HEADER = ("Port ID", "Old switch", "Old port", "New switch", "New port",
           "MAC addresses")
_ROW = ("P0001", "gx-01", "1/7", "leaf-01", "1/3", "aa:bb:cc:dd:ee:ff")


def _home(tmp_path: Path, monkeypatch) -> Path:
    """A home directory with a config and an inventory in it."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("dvr_controllers: []\n")
    (home / "switches.yaml").write_text(
        "switches:\n  - name: gx-01\n    platform: voss\n")
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    return home


# -------------------------------------------------------------- the snapshot


def test_the_collect_verb_cannot_swallow_a_file_name(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    inventory = home / "switches.yaml"
    before = inventory.read_text()

    with pytest.raises(SystemExit) as exit_code:
        cli.main(["collect", str(inventory), "--no-fabric", "--dry-run",
                  "-o", str(tmp_path / "out")])

    assert exit_code.value.code == CONFIG_ERROR
    assert inventory.read_text() == before, "the inventory was overwritten"


def test_a_snapshot_never_overwrites_something_that_is_not_one(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    inventory = home / "switches.yaml"
    before = inventory.read_text()

    code = cli.main(["--no-fabric", "--dry-run", "-s", "gx-01:voss",
                     "--save-snapshot", str(inventory), "-o", str(tmp_path / "out")])

    assert code == CONFIG_ERROR
    assert inventory.read_text() == before


def test_replacing_an_earlier_snapshot_is_still_allowed(tmp_path):
    path = tmp_path / "snapshot.json"
    snapshot_mod.save(path, [], FabricState())
    assert snapshot_mod.is_snapshot(path)
    snapshot_mod.check_target(path)          # the same name again: fine

    (tmp_path / "folder").mkdir()
    with pytest.raises(snapshot_mod.SnapshotError, match="directory"):
        snapshot_mod.check_target(tmp_path / "folder")
    assert not snapshot_mod.is_snapshot(tmp_path / "nothing-here.json")


# ------------------------------------------------------- the command override


@pytest.mark.parametrize("value", ['"show mlt\\nreset"', "'show mlt ; reset'",
                                   '"show mlt\\rreset"'])
def test_an_override_holding_two_commands_is_refused(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"commands:\n  'show mlt': {value}\n")
    with pytest.raises(ConfigError, match="more than one command"):
        load_config(path, require_fabric=False)


def test_an_ordinary_override_still_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("commands:\n  'show mlt': 'show mlt all'\n")
    cfg = load_config(path, require_fabric=False)
    assert cfg.command_overrides == {"show mlt": "show mlt all"}


# ------------------------------------------------------------------ the sheet


@pytest.mark.parametrize(("name", "sep"), [("semi.csv", ";"), ("comma.csv", ","),
                                           ("tab.csv", "\t")])
def test_a_csv_is_read_whatever_separator_excel_used(tmp_path, name, sep):
    path = tmp_path / name
    path.write_text(sep.join(_HEADER) + "\n" + sep.join(_ROW) + "\n",
                    encoding="utf-8")
    sheet = cabling_sheet.load(path)
    row = sheet.rows[0]
    assert (row.old_switch, row.old_port) == ("gx-01", "1/7")
    assert (row.new_switch, row.new_port) == ("leaf-01", "1/3")
    assert row.macs == ["aa:bb:cc:dd:ee:ff"]


def test_a_quoted_cell_keeps_its_commas(tmp_path):
    """The separator is read off the header, so quoted data cannot shift it."""
    path = tmp_path / "sheet.csv"
    path.write_text("Old switch,Old port,Port VLANs\n"
                    'gx-01,1/7,"10,20,30"\n', encoding="utf-8")
    assert cabling_sheet.load(path).rows[0].port_vlans == [10, 20, 30]
