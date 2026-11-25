# 0013 One repository: migration_check ships inside switch-migrator

**Status:** accepted, 2026-11-25. **Supersedes 0001's placement**, and the "stays a
separate package" halves of switch-migrator's D9 and D13 (see `council/DECISIONS.md`
D14, which records the same decision from switch-migrator's side). 0001's *dependency
rule* is untouched and now enforced by a test.

**Context.** 0001 put this tool in its own repository so the verification would be
usable without the migrator. That worked, and the repository then cost more than it
returned. Nobody ran `migration-check` on its own. switch-migrator's D13 made it a
required dependency, which meant three checkouts side by side, a second `pip install`
with `--no-index` to keep the unreserved PyPI names from being resolved, a
`vendor/migration-check/` directory in every bundle, three `__version__`s in every
bundle file name, and two test suites - one of which had to skip a test when the
checkouts were not neighbours. D13's own Critic wrote the recommendation this record
acts on: merge once it is clear the tool never runs alone.

**Decision.** `migration_check` is a package of the switch-migrator repository, beside
`switch_migrator`, and ships in the same distribution.

* **It is still usable by itself.** `migration-check` stays a console script
  (`snap | show | diff | watch`) of that same install, and the package imports nothing
  from `switch_migrator` - the arrow is still switch_migrator -> migration_check ->
  fabric_weft. That is no longer a promise in prose:
  `tests/test_mac_versions.py::test_migration_check_imports_nothing_from_switch_migrator`
  fails on the first import that breaks it, so the package can be lifted back out.
* **fabric-weft stays its own repository.** It is a parser library with its own tests
  and decision records, useful outside this toolchain, and it is the one piece here
  that something else might reasonably depend on. It is still installed from
  `../fabric-engine-parser` or `vendor/fabric-weft/`.
* **One version.** `migration_check.__version__` is switch-migrator's, asserted by a
  test against `switch_migrator.__version__` and `pyproject.toml`. It appears in each
  snapshot's `snapshot.json` as `tool`; snapshot *compatibility* was and stays
  `SNAPSHOT_FORMAT`, which this does not touch.
* **One test suite.** The tests moved in as `tests/test_mac_*.py` with their fixtures
  under `tests/fixtures/mac-check/`. The ERS capture they share with switch-migrator is
  now read from `tests/fixtures/ers` in this repository, so the test that skipped
  without a neighbouring checkout always runs.
* **These records stay.** They are this package's history and are still the reason its
  code looks the way it does. Repository-wide decisions live in `council/DECISIONS.md`.

**Consequences.** Install is two packages instead of three, and a bundle carries one
`vendor/` directory instead of two. The tool's own `tools/make-bundle.py` and its
separate bundle are gone - switch-migrator's bundle ships this package like any other
tracked file. `migration_check` keeps the four `ruff` ignores its own config carried, as
`per-file-ignores` scoped to the package, so switch-migrator's stricter bar (D11) is
unchanged; converging the two is for whoever next touches these files.

**What this does not change.** Everything 0002-0012 decided. In particular 0012's
contract - what switch-migrator's `--mac-snapshot` and `--verify-migration
--mac-baseline` do, and that the glue is `switch_migrator/mac_check.py` - holds as
written; only the `pip` arrangement under it changed.
