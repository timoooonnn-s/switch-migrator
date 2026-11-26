"""Where the tool's files come from when nobody says (switch_migrator.paths).

The tool is installed once and run from anywhere on the server, so no default
may depend on the working directory. Every test here fixes the search places
explicitly - never the machine's own ~/.config or /etc.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from switch_migrator import cli, paths
from switch_migrator.config import ConfigError, load_config

CONFIG_ERROR = 2


def _places(tmp_path: Path, monkeypatch, *,
            checkout: bool = True) -> tuple[Path, Path, Path]:
    """The three search places, all inside tmp_path: user, system, install.

    The real ~/.config, /etc and this checkout are never touched: the home
    directory, the system directory and the installation directory are all
    redirected into the test's own tmp_path.
    """
    me = tmp_path / "me"
    user = me / ".config" / "switch-migrator"
    system, installed = tmp_path / "etc", tmp_path / "install"
    for place in (user, system, installed):
        place.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda _cls: me))
    monkeypatch.setattr(paths, "SYSTEM_HOME", system)
    monkeypatch.setattr(paths, "install_dir", lambda: installed)
    monkeypatch.setattr(paths, "_installed_in_place", lambda: checkout)
    return user, system, installed


def _env(**values: str) -> dict[str, str]:
    """An environment with none of the tool's variables set but these."""
    return {k: v for k, v in values.items() if v}


def test_sm_home_is_the_home_and_nothing_else_is_consulted(tmp_path, monkeypatch):
    user, _system, _install = _places(tmp_path, monkeypatch)
    (user / paths.CONFIG_NAME).write_text("{}\n")
    env = _env(SM_HOME=str(tmp_path / "elsewhere"))

    assert paths.home(env) == tmp_path / "elsewhere"
    assert paths.looked_in(env) == [tmp_path / "elsewhere"]
    assert paths.config_file(env) == tmp_path / "elsewhere" / "config.yaml"


def test_the_user_config_wins_over_the_server_and_the_install_folder(tmp_path,
                                                                    monkeypatch):
    user, system, install = _places(tmp_path, monkeypatch)
    for place in (user, system, install):
        (place / paths.CONFIG_NAME).write_text("{}\n")
    assert paths.home(_env()) == user

    (user / paths.CONFIG_NAME).unlink()
    assert paths.home(_env()) == system
    (system / paths.CONFIG_NAME).unlink()
    assert paths.home(_env()) == install


def test_xdg_config_home_names_the_user_directory(tmp_path, monkeypatch):
    _places(tmp_path, monkeypatch)
    xdg = tmp_path / "xdg" / "switch-migrator"
    xdg.mkdir(parents=True)
    (xdg / paths.CONFIG_NAME).write_text("{}\n")
    env = _env(XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    assert paths.home(env) == xdg


def test_with_no_config_anywhere_a_checkout_is_the_home(tmp_path, monkeypatch):
    user, _system, install = _places(tmp_path, monkeypatch, checkout=True)
    assert paths.home(_env()) == install, "the checkout/bundle we run from"

    # installed as a wheel: site-packages is no place for a config.yaml, so the
    # user's config directory is where the error tells them to create one
    monkeypatch.setattr(paths, "_installed_in_place", lambda: False)
    assert paths.home(_env()) == user


def test_the_missing_config_message_names_every_place_tried(tmp_path, monkeypatch):
    user, system, install = _places(tmp_path, monkeypatch)
    message = paths.missing_config(paths.config_file(_env()), _env())
    for place in (user, system, install):
        assert str(place) in message
    assert "SM_HOME" in message
    assert "-c" in message


def test_a_missing_config_is_a_config_error_that_says_where_it_looked(tmp_path,
                                                                     monkeypatch):
    _places(tmp_path, monkeypatch)
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "nowhere"))
    code = cli.main(["--no-fabric", "--dry-run", "-s", "gx-01:voss",
                     "-o", str(tmp_path / "out")])
    assert code == CONFIG_ERROR


def test_the_reports_go_under_the_home_directory(tmp_path, monkeypatch):
    user, _system, _install = _places(tmp_path, monkeypatch)
    (user / paths.CONFIG_NAME).write_text("{}\n")
    assert paths.output_root(_env()) == user / "output"
    assert paths.output_root(_env(SM_OUTPUT_DIR=str(tmp_path / "reports"))) == \
        tmp_path / "reports"


def test_a_read_only_home_does_not_take_the_reports_with_it(tmp_path, monkeypatch):
    """/etc/switch-migrator for a non-root user: the run must not fail on the
    directory it was going to write its log into."""
    user, _system, _install = _places(tmp_path, monkeypatch)
    (user / paths.CONFIG_NAME).write_text("{}\n")
    monkeypatch.setattr(paths.os, "access", lambda *_a, **_k: False)
    assert paths.output_root(_env()) == tmp_path / "me" / "switch-migrator" / "output"


def test_a_relative_input_is_tried_here_first_and_in_the_home_second(tmp_path,
                                                                    monkeypatch):
    user, _system, _install = _places(tmp_path, monkeypatch)
    (user / paths.CONFIG_NAME).write_text("{}\n")
    (user / "switches.yaml").write_text("switches: []\n")
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)

    # not here, but in the home: found there
    assert paths.input_path(Path("switches.yaml"), _env()) == user / "switches.yaml"
    # here as well: the working directory wins, as it does for every command
    (here / "switches.yaml").write_text("switches: []\n")
    assert paths.input_path(Path("switches.yaml"), _env()) == Path("switches.yaml")
    # absolute, and nowhere at all: exactly as typed, so the error names it
    absolute = tmp_path / "nope.yaml"
    assert paths.input_path(absolute, _env()) == absolute
    assert paths.input_path(Path("nope.yaml"), _env()) == Path("nope.yaml")


def test_a_run_from_another_directory_finds_config_inventory_and_profiles(
        tmp_path, monkeypatch, capsys):
    """The point of all of it: cd anywhere, run the tool, get the same files."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("excluded_vlans: [1]\n")
    (home / "switches.yaml").write_text(
        "switches:\n  - name: gx-01\n    platform: voss\n")
    (home / "profiles.yaml").write_text(
        "profiles:\n  site-a:\n    inventory: switches.yaml\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv(paths.HOME_ENV, str(home))

    # no -c, no -i, no --profiles-file: everything comes from the home
    assert cli.main(["--no-fabric", "--dry-run", "--profile", "site-a"]) == 0
    printed = capsys.readouterr().err
    assert "gx-01" in printed
    assert not (elsewhere / "output").exists(), "nothing was written to the cwd"
    assert (home / "output" / "switch-migrator.log").is_file()


def test_where_prints_the_resolved_paths_and_writes_nothing(tmp_path, monkeypatch,
                                                            capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--where"]) == 0
    printed = capsys.readouterr().err
    assert str(home) in printed, "a path must never be wrapped across lines"
    assert "output" in printed
    assert not (home / "output").exists(), "--where creates no directory"
    assert sorted(p.name for p in home.iterdir()) == ["config.yaml"]


def test_the_config_names_its_own_file_so_the_inventory_beside_it_is_found(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("excluded_vlans: [1]\n")
    assert load_config(cfg_path, require_fabric=False).source == cfg_path


def test_a_config_that_is_a_directory_is_a_clean_error(tmp_path):
    (tmp_path / "config.yaml").mkdir()
    with pytest.raises(ConfigError, match="directory"):
        load_config(tmp_path / "config.yaml", require_fabric=False)


def test_the_test_session_runs_with_a_throwaway_home(tool_home):
    """conftest's safety net: the suite never reads the developer's config."""
    assert os.environ[paths.HOME_ENV] == str(tool_home)
    assert paths.home() == tool_home
