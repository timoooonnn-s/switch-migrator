"""Which site is a switch at, and which sites share a cabling worksheet."""

import pytest

from switch_migrator import location as L
from switch_migrator.config import ConfigError, load_config
from switch_migrator.models import Platform, PortState, SwitchAudit
from switch_migrator.report.migration import (
    build_cabling,
    build_cabling_by_location,
)

RULES = L.LocationRules(
    patterns={"gx-11-*": "Frankfurt DC1", "gx-12-*": "Frankfurt DC2",
              "mu-*": "Munich"},
    groups={"Frankfurt": ["Frankfurt DC*"]},
)


# --------------------------- resolving a location --------------------------

def test_a_configured_pattern_gives_the_site_its_real_name():
    assert L.locate("gx-11-s72-p1", RULES) == "Frankfurt DC1"
    assert L.locate("gx-12-s01-p9", RULES) == "Frankfurt DC2"
    assert L.locate("mu-01-a", RULES) == "Munich"


def test_patterns_are_matched_case_insensitively():
    assert L.locate("GX-11-S72-P1", RULES) == "Frankfurt DC1"


def test_the_first_matching_pattern_wins():
    rules = L.LocationRules(patterns={"gx-11-s72-*": "Frankfurt Room 72",
                                      "gx-11-*": "Frankfurt DC1"})
    assert L.locate("gx-11-s72-p1", rules) == "Frankfurt Room 72"
    assert L.locate("gx-11-s99-p1", rules) == "Frankfurt DC1"


def test_an_unconfigured_site_falls_back_to_the_name_prefix():
    """A switch at a site nobody has configured yet still groups sensibly
    instead of landing in a bucket called 'other'."""
    assert L.locate("be-42-s01-p1", RULES) == "be-42"


def test_the_fallback_segment_count_is_configurable():
    one = L.LocationRules(fallback_segments=1)
    three = L.LocationRules(fallback_segments=3)
    assert L.locate("gx-11-s72-p1", one) == "gx"
    assert L.locate("gx-11-s72-p1", three) == "gx-11-s72"


def test_underscores_count_as_separators_too():
    assert L.locate("gx_11_s72_p1", L.LocationRules()) == "gx-11"


def test_a_short_name_does_not_become_its_own_location():
    """'dvr-01' with the default 2 segments would otherwise put every single
    device in a location of its own, which is never what anyone wants."""
    assert L.locate("dvr-01", L.LocationRules()) == "dvr"
    assert L.locate("leaf-01", L.LocationRules()) == "leaf"
    assert L.locate("core", L.LocationRules()) == "core"


def test_an_empty_name_is_unassigned():
    assert L.locate("", RULES) == L.UNKNOWN
    assert L.locate("   ", RULES) == L.UNKNOWN


# --------------------------- grouping --------------------------------------

def test_grouped_locations_share_a_sheet():
    assert L.group_for("Frankfurt DC1", RULES) == "Frankfurt"
    assert L.group_for("Frankfurt DC2", RULES) == "Frankfurt"


def test_an_ungrouped_location_stands_alone():
    """'separate everything' is what you get without writing any groups."""
    assert L.group_for("Munich", RULES) == "Munich"
    assert L.group_for("be-42", RULES) == "be-42"


def test_groups_accept_exact_names_as_well_as_globs():
    rules = L.LocationRules(groups={"North": ["Munich", "be-42"]})
    assert L.group_for("Munich", rules) == "North"
    assert L.group_for("be-42", rules) == "North"
    assert L.group_for("Hamburg", rules) == "Hamburg"


def test_split_puts_every_switch_in_exactly_one_group():
    names = ["gx-11-s72-p1", "gx-11-s74-wu", "gx-12-s01-p9", "mu-01-a",
             "be-42-s01-p1"]
    groups = L.split(names, RULES)
    assert groups == {
        "be-42": ["be-42-s01-p1"],
        "Frankfurt": ["gx-11-s72-p1", "gx-11-s74-wu", "gx-12-s01-p9"],
        "Munich": ["mu-01-a"],
    }
    assert sum(len(v) for v in groups.values()) == len(names)


def test_unassigned_sorts_last():
    groups = L.split(["mu-01-a", "", "gx-11-s72-p1"], RULES)
    assert list(groups)[-1] == L.UNKNOWN


# --------------------------- the per-run override --------------------------

def test_parse_group_args():
    assert L.parse_group_args(["Frankfurt=gx-11,gx-12", "South=mu-01"]) == {
        "Frankfurt": ["gx-11", "gx-12"], "South": ["mu-01"]}
    assert L.parse_group_args(["A = x , y "]) == {"A": ["x", "y"]}
    assert L.parse_group_args([]) == {}


def test_a_mistyped_group_is_refused_not_silently_empty():
    """A typo that produced an empty group would scatter a site's ports across
    several worksheets without saying so."""
    for bad in ["Frankfurt", "=gx-11", "Frankfurt=", "  =  "]:
        with pytest.raises(ValueError, match="NAME=location1"):
            L.parse_group_args([bad])


# --------------------------- config ----------------------------------------

def test_locations_load_from_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "locations:\n"
        "  patterns:\n"
        "    'gx-11-*': Frankfurt DC1\n"
        "    'gx-12-*': Frankfurt DC2\n"
        "  fallback_segments: 3\n"
        "  groups:\n"
        "    Frankfurt: ['Frankfurt DC1', 'Frankfurt DC2']\n")
    cfg = load_config(path, require_fabric=False)
    assert cfg.locations.patterns["gx-11-*"] == "Frankfurt DC1"
    assert cfg.locations.fallback_segments == 3
    assert cfg.locations.groups == {"Frankfurt": ["Frankfurt DC1",
                                                  "Frankfurt DC2"]}
    assert L.group_of_switch("gx-12-s01-p9", cfg.locations) == "Frankfurt"


def test_a_config_without_locations_still_works(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    cfg = load_config(path, require_fabric=False)
    assert not cfg.locations.configured
    assert L.locate("gx-11-s72-p1", cfg.locations) == "gx-11"


def test_a_single_group_member_may_be_written_as_a_string(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("locations:\n  groups:\n    Frankfurt: Frankfurt DC1\n")
    cfg = load_config(path, require_fabric=False)
    assert cfg.locations.groups == {"Frankfurt": ["Frankfurt DC1"]}


def test_a_malformed_locations_section_is_a_clear_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("locations: [a, b]\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(path, require_fabric=False)
    path.write_text("locations:\n  groups:\n    Frankfurt: []\n")
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(path, require_fabric=False)


# --------------------------- the split in the sheet ------------------------

def _audit(name: str) -> SwitchAudit:
    a = SwitchAudit(name=name, host=name, platform=Platform.VOSS, reachable=True)
    a.ports = [PortState(port="1/1", oper_up=True, vlans=[100]),
               PortState(port="1/2", oper_up=True, vlans=[200])]
    return a


def test_the_cabling_sheet_splits_into_one_table_per_group():
    audits = [_audit("gx-11-s72-p1"), _audit("gx-12-s01-p9"), _audit("mu-01-a")]
    tables = build_cabling_by_location(audits, RULES)
    assert [t.title for t in tables] == ["Cabling Frankfurt", "Cabling Munich"]
    assert len(tables[0].rows) == 4          # two switches, two ports each
    assert len(tables[1].rows) == 2


def test_no_link_appears_on_two_sheets():
    """This is a fill-in document: a row on two sheets means two technicians
    can answer it and one set of answers is lost."""
    audits = [_audit("gx-11-s72-p1"), _audit("mu-01-a")]
    combined = build_cabling(audits)
    tables = build_cabling_by_location(audits, RULES)

    def keys(table):
        old_switch = table.headers.index("Old switch")
        old_port = table.headers.index("Old port")
        return [(r[old_switch], r[old_port]) for r in table.rows]

    split_keys = [k for t in tables for k in keys(t)]
    assert len(split_keys) == len(set(split_keys)), "a link is on two sheets"
    assert sorted(split_keys) == sorted(keys(combined)), "a link went missing"


def test_the_split_keeps_every_column_of_the_combined_sheet():
    audits = [_audit("gx-11-s72-p1")]
    assert (build_cabling_by_location(audits, RULES)[0].headers
            == build_cabling(audits).headers)


def test_without_configured_locations_the_split_falls_back_to_name_prefixes():
    audits = [_audit("gx-11-s72-p1"), _audit("gx-12-s01-p9")]
    titles = [t.title for t in
              build_cabling_by_location(audits, L.LocationRules())]
    assert titles == ["Cabling gx-11", "Cabling gx-12"]
