"""Which site does a switch live at, and which sites share a worksheet?

Switch names carry the location in their prefix (`gx-11-s72-p1` sits at
`gx-11`), so the cabling sheet can be split per site without anyone maintaining
a second list of which box is where.

Two steps, deliberately separate:

  location   the site a switch belongs to. Resolved from configured name
             patterns first - so the sheet is headed "Frankfurt DC1" rather
             than "gx-11" - and from the leading name segments when no pattern
             matches, so a switch at a site nobody has configured yet still
             lands somewhere sensible instead of in a bucket called "other".

  group      the locations that share one worksheet. Two halves of the same
             campus usually want one sheet; two cities never do. A location in
             no group is its own group, which makes "separate everything" the
             behaviour you get for free.

The split is driven off the OLD switch name, because the cabling sheet is
written before anyone knows the new one - the NEW columns are what the
technicians fill in.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

UNKNOWN = "unassigned"


@dataclass
class LocationRules:
    """How to turn a switch name into a location, and locations into groups."""
    # switch-name glob -> location name, in order; the first match wins
    patterns: dict[str, str] = field(default_factory=dict)
    # leading '-' segments to use when no pattern matches (gx-11-s72-p1 -> gx-11)
    fallback_segments: int = 2
    # group name -> the locations (names or globs) that share one worksheet
    groups: dict[str, list[str]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.patterns or self.groups)


def locate(name: str, rules: LocationRules) -> str:
    """The location of one switch."""
    name = (name or "").strip()
    if not name:
        return UNKNOWN
    lowered = name.lower()
    for pattern, location in rules.patterns.items():
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return location
    segments = re.split(r"[-_]", name)
    n = max(1, rules.fallback_segments)
    if len(segments) <= n:
        # the whole name is shorter than the rule - 'dvr-01' with n=2 would
        # otherwise become its own location per device, which is never useful
        return segments[0] if len(segments) > 1 else name
    return "-".join(segments[:n])


def group_for(location: str, rules: LocationRules) -> str:
    """The worksheet a location belongs on. Ungrouped locations stand alone."""
    for group, members in rules.groups.items():
        for member in members:
            if location.lower() == str(member).lower() or fnmatch.fnmatch(
                    location.lower(), str(member).lower()):
                return group
    return location


def group_of_switch(name: str, rules: LocationRules) -> str:
    return group_for(locate(name, rules), rules)


def split(names: list[str], rules: LocationRules) -> dict[str, list[str]]:
    """{group: [switch names]}, groups in name order, unassigned last."""
    out: dict[str, list[str]] = {}
    for name in names:
        out.setdefault(group_of_switch(name, rules), []).append(name)
    return {k: out[k] for k in sorted(out, key=lambda g: (g == UNKNOWN, g.lower()))}


def parse_group_args(values: list[str]) -> dict[str, list[str]]:
    """Parse repeated --location-group 'NAME=loc1,loc2' arguments.

    Raises ValueError with a usable message rather than silently producing an
    empty group, because a mistyped one would quietly scatter a site's ports
    across several worksheets.
    """
    groups: dict[str, list[str]] = {}
    for raw in values or []:
        name, sep, members = raw.partition("=")
        if not sep or not name.strip() or not members.strip():
            raise ValueError(
                f"--location-group '{raw}': expected NAME=location1,location2 "
                f"(e.g. 'Frankfurt=gx-11,gx-12')")
        groups[name.strip()] = [m.strip() for m in members.split(",") if m.strip()]
    return groups


def describe(rules: LocationRules, names: list[str]) -> list[str]:
    """One line per group for the console: what ended up where."""
    return [f"{group}: {', '.join(sorted(members))}"
            for group, members in split(names, rules).items()]
