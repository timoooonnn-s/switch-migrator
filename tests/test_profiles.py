"""Named scenario profiles: file round-trip and session application."""

from pathlib import Path

import pytest

from switch_migrator import profiles as P


def test_missing_file_means_no_profiles(tmp_path):
    assert P.load_profiles(tmp_path / "profiles.yaml") == {}


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "profiles.yaml"
    P.save_profile("site-a", {
        "inventory": Path("inventories/site-a.yaml"),
        "output_dir": Path("output/site-a"),
        "new_switch": "leaf-a-01",
        "save_raw": True,
        "split_by_location": True,
        "location_groups": {"Frankfurt": ["gx-11", "gx-12"]},
    }, path)
    profiles = P.load_profiles(path)
    assert set(profiles) == {"site-a"}
    prof = profiles["site-a"]
    assert prof["inventory"] == Path("inventories/site-a.yaml")
    assert prof["output_dir"] == Path("output/site-a")
    assert prof["new_switch"] == "leaf-a-01"
    assert prof["save_raw"] is True
    assert prof["location_groups"] == {"Frankfurt": ["gx-11", "gx-12"]}


def test_saving_a_second_profile_keeps_the_first(tmp_path):
    path = tmp_path / "profiles.yaml"
    P.save_profile("a", {"new_switch": "one"}, path)
    P.save_profile("b", {"new_switch": "two"}, path)
    profiles = P.load_profiles(path)
    assert set(profiles) == {"a", "b"}
    assert profiles["a"]["new_switch"] == "one"


def test_a_typoed_key_is_an_error_not_a_silent_drop(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text("profiles:\n  a:\n    new_swich: leaf-01\n")
    with pytest.raises(P.ProfileError, match="new_swich"):
        P.load_profiles(path)
