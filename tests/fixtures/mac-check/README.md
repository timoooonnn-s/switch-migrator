# Test fixtures

A snapshot folder per source, one file per command, named with switch-migrator's slug
rule (decision 0002), so each folder is read as a switch-migrator raw folder.

| Folder | Source | Evidence |
|---|---|---|
| `fe-lab2/` | fabric-weft `tests/fixtures/fe-9.4-gns3-lab2-fe-0X/`, the eight Fabric Engine commands, renamed only. GNS3 lab, FEGNS3.9.4.0.0.GA, 2026-11-22: CVLAN and ELAN I-SIDs, end devices with traffic, VRF blue on fe-03/fe-04. | **Real** (lab) |
| `fe-lab6/` | fabric-weft `fe-9.4-gns3-lab6-fe-0X/`, same four nodes after a clean rebuild. The end devices had not sent traffic yet, so no edge MAC is learned. | **Real** (lab) |

Not copied, read in place:

- switch-migrator `tests/fixtures/ers/`: BOSS output from work (trusted, user
  2026-11-23). `tests/test_boss.py` skips when the checkout is not next to this one.
  Work output never goes into this repository (R8).
- `BOSS_MAC_SAMPLE` in `tests/conftest.py`: switch-migrator's three-row real
  `show mac-address-table` sample, verbatim.

Synthetic: `tests/test_compare.py` builds its BOSS → Fabric Engine scenario from model
records with invented values. It proves the verdict logic, not any parser.
