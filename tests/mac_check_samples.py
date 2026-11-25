"""Fixture paths and inline captures for the migration_check tests.

These were pre-migration-checker's `tests/conftest.py` (D14). Two things changed
when the package moved in: the Fabric Engine lab snapshots now live under
`tests/fixtures/mac-check/`, and the ERS capture is read from this repository's
own `tests/fixtures/ers` instead of a sibling checkout - so the test that used to
skip when the two repositories were not side by side always runs.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MAC_CHECK = FIXTURES / "mac-check"
FE_LAB2 = MAC_CHECK / "fe-lab2"
FE_LAB6 = MAC_CHECK / "fe-lab6"

# Work captures, trusted (user, 2026-11-23). Read in place, never copied (R8).
ERS_CAPTURE = FIXTURES / "ers"

# `tests/test_migration_sheets.py` ERS_MACS, cut to three rows on purpose, so the
# address count in the header does not match what the table holds (0007: lenient
# and loud - a short table is read, and said so).
BOSS_MAC_SAMPLE = """\
Mac Address Table Aging Time: 300
Number of addresses: 1655

   MAC Address    Vid   Type       Source
----------------- ---- ------- --------------
0011.2200.0011       735 Dynamic Port:7
0011.2200.0012       735 Dynamic Port:8
0011.2200.0013       174 Dynamic Trunk:1
"""
