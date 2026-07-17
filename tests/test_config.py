from pathlib import Path

import pytest

from switch_migrator.config import ConfigError, load_config

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
