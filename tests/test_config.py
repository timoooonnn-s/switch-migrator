from pathlib import Path

import pytest

from switch_migrator.config import ConfigError, load_config, load_inventory

MINIMAL = """
dvr_controllers:
  - name: dvr-01
    host: 10.0.0.1
isid_conventions:
  offsets: [10000]
"""


def _write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL + extra)
    return path


def test_excluded_vlan_names_parsed(tmp_path: Path):
    cfg = load_config(_write(tmp_path, """
excluded_vlans: [1, 4000]
excluded_vlan_names:
  - "quarant*"
  - "mgmt"
"""))
    assert cfg.excluded_vlans == {1, 4000}
    assert cfg.excluded_vlan_names == ["quarant*", "mgmt"]


def test_vlan_name_in_excluded_vlans_gives_helpful_error(tmp_path: Path):
    # this is exactly the mistake that happens in practice: putting the
    # quarantine VLAN's NAME into the numeric id list
    with pytest.raises(ConfigError, match="excluded_vlan_names"):
        load_config(_write(tmp_path, """
excluded_vlans: [1, quarantaine]
"""))


NO_FABRIC = """
switches: []
"""


def test_no_fabric_makes_dvr_and_conventions_optional(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("excluded_vlan_names: [\"quarant*\"]\n")
    # default (require_fabric=True) still demands a fabric...
    with pytest.raises(ConfigError):
        load_config(path)
    # ...but --no-fabric mode does not
    cfg = load_config(path, require_fabric=False)
    assert cfg.dvr_controllers == []
    assert cfg.isid_offsets == [] and cfg.isid_explicit == {}
    assert cfg.excluded_vlan_names == ["quarant*"]


# ------------------- default inventory / commands / console server ----------

def test_config_inventory_key_is_relative_to_the_config_file(tmp_path):
    from switch_migrator.config import default_inventory
    cfg_dir = tmp_path / "deploy"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("inventory: switches-prod.yaml\n")
    cfg = load_config(cfg_path, require_fabric=False)
    assert cfg.inventory == cfg_dir / "switches-prod.yaml"
    assert default_inventory(cfg) == cfg_dir / "switches-prod.yaml"


def test_default_inventory_falls_back_to_switches_yaml(tmp_path, monkeypatch):
    from switch_migrator.config import default_inventory
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("excluded_vlans: [1]\n")
    cfg = load_config(cfg_path, require_fabric=False)
    monkeypatch.chdir(tmp_path)
    assert default_inventory(cfg) is None          # nothing to find
    (tmp_path / "switches.yaml").write_text("switches: []\n")
    assert default_inventory(cfg) == Path("switches.yaml")


def test_command_overrides_must_stay_read_only(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("commands:\n  'show mlt': 'reset'\n")
    with pytest.raises(ConfigError, match="read-only"):
        load_config(cfg_path, require_fabric=False)
    cfg_path.write_text("commands:\n  'show mlt': 'show mlt all'\n")
    cfg = load_config(cfg_path, require_fabric=False)
    assert cfg.command_overrides == {"show mlt": "show mlt all"}


def test_console_server_settings_resolve_both_flavors(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "console_server:\n"
        "  host: tsserver\n"
        "  username_template: '{username}:70{port}'\n")
    cfg = load_config(cfg_path, require_fabric=False)
    assert cfg.console_server.configured
    # Avocent-style: the line rides in the username, TCP port stays 22
    assert cfg.console_server.resolve("admin", "03") == ("admin:7003", 22)

    cfg_path.write_text(
        "console_server:\n"
        "  host: tsserver\n"
        "  tcp_port_template: '70{port}'\n")
    cfg = load_config(cfg_path, require_fabric=False)
    # OpenGear-style: one TCP port per line, username unchanged
    assert cfg.console_server.resolve("admin", "03") == ("admin", 7003)


def test_inventory_console_line_is_read(tmp_path):
    inv = tmp_path / "inv.yaml"
    inv.write_text("switches:\n"
                   "  - name: sw-1\n    platform: ers\n    console: 12\n"
                   "  - name: sw-2\n    platform: voss\n")
    targets = load_inventory(inv)
    assert targets[0].console == "12"
    assert targets[1].console == ""
