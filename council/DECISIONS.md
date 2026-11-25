# Decisions

Each decision: what, why, who objected and what happened to the objection.
Decisions marked **(draft)** depend on an open question in `OPEN-QUESTIONS.md`
and may change when it is answered.

---

## D1 — Scope: DC migrations onto flex-UNI Fabric Engine leaves (2026-11-19)

Source platforms: ERS (main case) and Fabric Engine/VOSS. Target: flex-UNI
Fabric Engine leaves, usually as an SMLT pair, usually fewer switches than
before. Office/branch (90 %+ 1:1) is **out of scope**; the existing outputs
keep working for it, but no new work is done for it.

*Critic:* the old VOSS→VOSS "verbatim extract" was built for 1:1 moves and
is wrong for consolidation. **Accepted:** it stays for office/branch use, is
labelled as such, and is not the DC path.

## D2 — The migration plan is the spine (built 2026-11-24, `plan.py`)

A `MigrationPlan` maps every in-use old link to a new switch and port. All
DC outputs (config, MLT blocks, sheets, verification, AixBOMS changes) are
renderings of facts + plan + standard. The plan is a file (YAML) that a human
can edit and that the Excel sheet round-trips into.

The allocator encodes the user's manual rule:

1. **Infrastructure is never allocated** — uplinks to the core, vIST, NNI,
   and ports reserved on the new model (R1).
2. **Dual-homed attachments** — real MLTs whose links reach both new
   switches, and hosts seen on two old switches/units — get ports **from the
   bottom up, at the same port number on both peers**. Same number on both
   peers is what makes the SMLT's MLT id (`4xx`) identical on both, which
   SMLT requires.
3. **Single-homed attachments** get ports **from the top down** (last access
   port of the model first).
4. When the two blocks meet, the pair is full: the plan says how many ports
   are left over. It never silently overflows.
5. **Deterministic and pinnable.** Same input, same plan. A port a human
   has fixed in the plan file is kept; the allocator fills around it.

How a host is recognised on two ports, best evidence first: the old MLT ·
the same AixBOMS end device · the same LLDP system name · overlapping learned
MACs. Weaker evidence is shown, never acted on silently.

*Field Engineer:* "the allocator must never move something I pinned."
**Adopted** as rule 5.
*Fabric Architect:* "an SMLT leg on only one peer is the worst outcome,
because it looks redundant and isn't." **Adopted:** the plan validator
refuses a dual-homed attachment that has a leg on only one peer.

## D3 — Config is produced per NEW switch, in three blocks only (draft: R9–R11)

One file per new switch, containing **only**:

1. **I-SID services** — `i-sid <id> elan` with `c-vid <vlan> port …` and
   `untagged-traffic port …`, on the **new** port numbers;
2. **port config** — per new port, in the two phases the switch itself uses
   (phase I `encapsulation dot1q`, phase II `flex-uni enable` and the access
   hygiene set from the profile);
3. **MLT / SMLT** — `4xx` id, LACP key = MLT id, members per peer.

Everything ZTP owns (domain, DNS, syslog, SSH, SNMP, mgmt) is excluded *by
construction*: the renderer has no code path that could emit it. When a
baseline capture of the new switch exists (C1), it is used as a check:
I-SIDs that already exist are not created again, and NNI / vIST /
`auto-sense` ports are refused.

I-SID per VLAN, in order: explicit decision → the fabric's existing I-SID
(DvR read) → `port-preparer`'s rule for a new service (lower prefix of the
environment) **only if** a human has named the environment → otherwise a
`[REVIEW]` placeholder, commented out.

*Critic:* LACP member-port syntax on VOSS is still unverified (no real
running-config of an LACP port). **Accepted:** no LACP line is emitted until
C2 or C4 arrives and the syntax is pinned in a test.

### D3 — built 2026-11-25 (`new_switch_config.py`)

As decided above, with these choices:
* It renders from facts + plan (old VLAN bindings, new ports), not from the
  sheet's text cells: a re-plan with `--pins` from the snapshot regenerates it.
* Three kinds of line no capture shows yet are emitted but marked
  `[UNVERIFIED]` and listed in the header: `c-vid … mlt`, `encapsulation
  dot1q` on an MLT, and LACP member ports. *Critic:* D3 said "no LACP line
  until C2/C4" - **revised**: `interface mlt / lacp enable key` IS in a real
  capture (MLT 197), only the member-port side is unknown, so that part is
  marked instead of withheld.
* A VLAN with unknown tagging is emitted as a tagged c-vid and named as a
  PROBLEM on that port, not hidden.
* An old port that was admin-down stays `shutdown`, flagged.
* Only flex-UNI (I-SID) output. R9's "VLAN config by user choice" is not built:
  untagged membership in the platform-VLAN model is not in any capture.

## D4 — The company standard comes from port-preparer (revised 2026-11-24)

**Revised after R12:** port-preparer was an experiment and will not be used, so
its conventions are **copied** into `switch_migrator/standard.py` (I-SID rule,
reserved VLANs, MLT 4xx, LACP key = MLT id, MLT name template). The VLAN-name
codec was not copied: flex-UNI creates no VLAN names. The text below is the
original draft, kept for the record.

`switch-migrator` does not hold its own copy of the conventions. I-SID,
MLT id, LACP key, VLAN-name and reserved-object rules, and the access-port
profile, come from `port-preparer`. Start as a library dependency; extract a
shared package once it is clear which pieces both use.

*Software Engineer:* objects to a dependency on a tool that is itself at
"Phase 1". **Recorded, not adopted:** the conventions module is the stable,
tested part of `port-preparer`; the alternative is two copies that drift.
Revisit if `port-preparer` changes those APIs.

## D5 — AixBOMS is read from its export files, never scraped (draft: R13–R17)

An importer reads the AixBOMS Excel/CSV export. Because the same kind of
value appears in different columns from row to row (`ID`, `von Kind ID`,
`von Kind2 ID`, `End ID`, `End Elternidentität 1–3` hold a rack location,
a switch, `switch(unit)x(port)` or a host name, depending on the record),
the importer does **not** map columns to meanings. It:

1. classifies every value by its shape — `switch(unit)x(port)`, a switch
   name from the inventory, a rack location, otherwise a host name;
2. finds the side of the row that is one of *our* old switches;
3. reads the other side as the far end: device, device port, rack;
4. keeps the patch cable numbers (`von Port Patchkabel`, `End Port
   Patchkabel`) and the medium (`von Port`);
5. merges fibre TX/RX rows of the same port;
6. puts every row it cannot place on an **"unplaced rows"** list — never
   drops one.

Then reconciliation per (old switch, port): **agree** · **doc-only** (hidden
host: it gets a new port in the plan like any other, and is flagged "verify
at the rack") · **switch-only** (AixBOMS is missing it) · **conflict** (the
doc says host A, LLDP says host B).

After the migration the tool writes the **AixBOMS changes** list — every
record to edit by hand, old value → new value — because AixBOMS has no API.

*Documentation Steward:* browser automation would save the export step.
**Rejected:** needs credentials, breaks on UI changes, and the export is one
click.

## D6 — Two sheets: rack sheet and engineer sheet

* **Rack sheet** — per old switch, in **faceplate order** (numeric port
  sort, unit by unit), narrow enough to print: Port ID · old port · **patch
  cable no.** · what is on it (AixBOMS name, else LLDP, else port name) ·
  new switch/port · ✔ · note. Hidden hosts are on it with "verify at rack".
* **Engineer sheet** — everything else (VLAN → I-SID, tagging, MLT, MACs,
  usage evidence, provenance, reconciliation status).

Both are renderings of the plan. Editing the new port on either one and
reading it back updates the plan file.

## D7 — No big-bang rewrite

The new spine (`model/plan.py`, `workflow/`) is built next to the existing
code. A module moves into the new structure when the new spine touches it.
`PortState` is split into facts / derived / plan as part of phase 1, because
the plan cannot be built on the mutable god-object.

*Software Engineer:* wanted the restructure first. *Field Engineer:* wanted
features first. **Chair:** the plan is both — it is the first new feature
and it forces the one split that matters. Everything else follows when
touched.

## D9 — migration-check is the MAC-level verification (2026-11-23) — *amended by D13 and D14*

`pre-migration-checker` (`migration-check`) stays a separate package; the
arrow is switch-migrator → migration-check → fabric-weft, never back (its own
decisions 0001/0012). It is an **optional** dependency: without it everything
else works and the MAC options say how to install it. It is not declared in
`pyproject.toml`, because it is not on PyPI and a same-named package there
would be installed instead (dependency confusion).

> **D14:** the package is no longer separate - it lives in this repository, as
> `migration_check/`. The *arrow* is unchanged and now has a test. The
> dependency-confusion point below applies to `fabric-weft` alone.

* **One session per switch.** Its commands ride on the session the collection
  already has open (`mac_check.SnapshotRecorder`), and every command the
  collection already sent is reused (`AnswerMemo`). ERS: zero extra commands;
  VOSS: three or four.
* **Baselines are taken by switch-migrator** (`--mac-snapshot DIR`), with
  migration-check's own command list. That closes both gaps its 0.2.0 left:
  old BOSS gear (legacy SSH) and old VOSS (`show vlan mac-address-entry`
  instead of `fdb-entry`).
* **After side**: `--verify-migration --mac-baseline DIR`, optionally
  `--mac-watch` (its `watch()`, paced by `mac_check:` in config.yaml; Ctrl-C
  ends the watch, not the run).
* Menu: option 2 offers the snapshot, option `v` offers it as the baseline.
* `--dry-run` lists the added commands; every one is checked to be a `show`.

*Software Engineer:* the menu and the CLI still each drive collection
themselves; this adds a shared `run_mac_check` but does not fix D7's
underlying duplication. **Accepted** — D7 fixes it when the workflow layer
lands.
*Critic:* a `--save-raw` folder of old VOSS switches is still not a usable
baseline. **Accepted:** the run names such folders before it starts, and the
README says to take VOSS baselines with `--mac-snapshot`. Teaching
migration-check to read `fdb-entry` is its own backlog item, and needs a real
capture first.

## D7 — built 2026-11-25: the workflow layer

`switch_migrator/workflow/` is the pipeline: `collect.py` (runners, per-switch
collection, fabric, dry run), `report.py` (compare, sheets, plan, health,
verification -> tables, and the exit-code rule), `outputs.py` (every file),
`macs.py` (the MAC-check steps). It takes small option types
(`CollectOptions`, `ReportOptions`, `OutputOptions`), never an argparse
namespace. `cli.py` turns flags into them; `menu.py` turns its session into
them - `Session.to_args`, the fake namespace, is gone, and the menu imports
nothing from `cli`. `manifest.build` takes a plain mapping of options.

Checked by the 461 tests and by replaying ten CLI runs over the fixtures
before and after: every sheet, config, commands file, manifest, log and exit
code identical. One intended change: with `--split-by-location` the per-site
Cabling tabs now come right after Port Info instead of after the plan sheets.
A verification sheet that does not load now fails before any switch is read
(exit 2), not after the collection.

Still open from D7: the PortState split (facts / derived / plan).

## D11 — Lint and type checks (2026-11-25) — *tightened by D12*

`pytest && ruff check . && mypy` all pass and are the bar for a change
(config in `pyproject.toml`, tools in the `dev` extra).

* **ruff:** pre-migration-checker's rule families. Ignored, with a reason in
  the config: magic values, early-return tables, many arguments, imports
  inside functions (optional dependencies). Complexity capped at 18 branches /
  60 statements; four decision tables carry an explained `noqa`
  (`_collect_voss`, `_verify_row`, `run_menu`, `generate_voss_from_ers`).
  `load_config` and `_enrich_migration_fields` were split instead.
* **mypy:** not strict - `check_untyped_defs` on an untyped-by-default code
  base. migration-check / fabric-weft are skipped (optional, Python 3.11).
* **Not done:** `ruff format`. The code base was never formatted by a tool;
  formatting it now would rewrite most lines and bury every real change of the
  next weeks in the diff. Worth doing once, on its own, when nothing else is
  in flight.

What the checks found that mattered: closures over loop variables in the LLDP
summary parser, row/severity `zip`s that would drop rows silently, an optional
console-server setting dereferenced unchecked, one variable holding two types
in the ERS generator, a module constant used before its definition, and the
collectors typed for a runner when the MAC check hands them a memo (now a
`CommandSource` protocol). Behaviour unchanged: the same fixture replay as
D7, every file identical.

## D12 — Review round 2 fixed; no suppressions left (2026-11-25)

The council's second breakdown, fixed in the code:

* **Extract:** the old VOSS extract leaves out what a ZTP'd switch already
  has or what names the old hardware's ports (the SPB B-VLANs, `vlan members
  remove 1 1/1-1/48,...`) and lists them under `LEFT OUT`. It still carries the
  old port numbers and says so: `--plan` is the route for a move.
* **ERS draft:** only stack unit 1's access ports keep their number as 1/N;
  other units and ports above the access range are flagged, never renumbered.
  The access range comes from `plan.ports` / `plan.uplinks` (48 assumed, and
  said so, when unset); per-port lines come from `plan.port_lines`.
* **I-SID worksheet:** the `explicit` snippet is no longer pre-filled with the
  first candidate. Every line is commented out with `<ISID>`, so pasting it
  unread decides nothing.
* **Version check:** on start the tool says when the checkout is behind its
  upstream, from the last fetch only (no network, no writes).
* **Port IDs** survive a re-plan: with `--pins` the sheet's IDs are kept and
  new ports get numbers above the highest.
* **Verbs:** `collect`, `plan`, `verify` as shorthands for their flags (PLAN.md's
  three verbs); every flag still works.
* Docstrings that told the story of a past bug now say what the code does.

**Checks, tightened from D11:** ruff at its default complexity limits over the
package *and* the tests, with no ignore list and no `noqa`; mypy `strict` with
no `type: ignore`. Long functions were split along their own steps (the VLAN
comparison is now a list of rules tried in order, the DvR and ERS-config
readers are tables, the menu's actions and the new-switch renderer are one
function per section), long parameter lists became small dataclasses
(`handover.Contents`, `connection.Device` / `RunnerSettings`,
`menu.MenuStart`), and every number a test checks has a name or is derived
from the fixture. Two limits the tests check are now named in the package:
`config.DEFAULT_MAC_CAP`, `report.excel.SHEET_TITLE_MAX`.

Behaviour: 468 tests pass (453 + 1 skip without migration-check). The fixture
replay from D7, run again, differs from the last one only where the fixes
above meant it to (extract `LEFT OUT`, worksheet placeholders, ERS
numbering note) and in log timestamps.

**Not done, on purpose:**
* *The PortState split* (facts / derived / plan), open since D7. The plan
  already lives in its own model; nothing today needs PortState to be
  immutable. **Recommendation:** do it when the AixBOMS import (phase 2) adds a
  second source of facts per port - that is the first time "where did this
  value come from" has two answers.
* *Raw captures inside snapshots.* A snapshot that carried the raw CLI output
  would also carry the running-config, which the collection drops on purpose
  unless it was asked for (secrets). **Recommendation:** keep snapshots to the
  parsed state; `--save-raw` stays the separate, explicit way to keep raw
  output - and it already leaves the running-config out unless the config
  was asked for.
* `ruff format`: still as in D11.

## D13 — One bundle, Python 3.11, migration-check required (2026-11-25)

* **Python 3.11** (the work server: Linux x86_64, 3.11 - R18). `requires-python`,
  ruff and mypy targets; the three `(str, Enum)` classes are `StrEnum`. The
  fixture replay differs only in paths and timestamps.
* **migration-check is required**, `migration-check>=0.2,<0.3` in
  `pyproject.toml` (and migration-check declares `fabric-weft>=0.1,<0.2`). With
  both on 3.11, D9's reason for optional was gone. The runtime gate
  (`problem()` / `available()` / `require()`, the version check, the import
  fallback) is removed; pip guarantees the version, and mypy now checks the
  calls instead of skipping the package.
* **D9's dependency-confusion point still stands**: the names are not reserved
  on PyPI. Our three packages are installed only with `--no-index` - in the
  bundle's second pip step and in the README. A plain `pip install .` into a
  fresh venv would fetch them from PyPI and must not be used. *Closing it for
  good* (placeholder registrations or an internal index) is the user's action
  and **open**.
* **One bundle** (`tools/make-bundle.py`): switch-migrator plus
  `vendor/migration-check/` and `vendor/fabric-weft/`, built from the sibling
  checkouts. It prints the install in order: third-party packages from the
  index with `-c constraints.txt`, then our three with `--no-index
  --no-build-isolation` (the user chose: pip fetches the dependencies, the
  bundle prints the commands rather than running them).
* **`constraints.txt`**: every third-party version, compiled for Linux x86_64 /
  Python 3.11. Tested: a fresh 3.11 venv with pip 24.0 installs from it, `pip
  check` is clean, and the suite passes from the unpacked copy (468 + the 4
  bundle-builder tests, which need git). Replace it with the server's `pip
  freeze --all --exclude-editable` once a migration ran there.

**Not merged.** The repositories stay separate (D9; migration-check's 0001 and
0012 - it is meant to stand alone and maybe become a TRuffle command).
*Critic:* three repos for one person's toolchain is overhead if nobody runs
migration-check on its own. **Recommendation:** revisit after a few real
migrations - if most migration-check changes need a matching `mac_check.py`
change and it never ran alone, merge it then.

> **Superseded the same day by D14**, which does what the Critic recommended.
> The user did not wait for the few real migrations: the three-checkout install
> was already the most awkward part of D13, and nothing about it was going to
> read better after a window at 3 a.m. Everything else in D13 stands - Python
> 3.11, one bundle, `constraints.txt`, and the open PyPI-name item, which now
> concerns `fabric-weft` only.

## D14 — migration-check moves in; it still runs on its own (2026-11-25)

**Supersedes** the "stays separate" halves of D9 and D13, and
`pre-migration-checker`'s own 0001 placement. Recorded there as
[decision 0013](../docs/decisions/0013-one-repository.md).

`migration_check` is a package of **this** repository, beside `switch_migrator`,
and ships in the same distribution. Nobody ran it alone, and D13's required-
dependency arrangement cost: three checkouts side by side, a second `pip`
invocation with `--no-index` so the unreserved PyPI names could not be hijacked,
`vendor/migration-check/` in every bundle, three `__version__`s in every bundle
name, and two suites - one of which skipped a test when the checkouts were not
neighbours.

**It is still usable by itself.** That was 0001's whole reason and it survives:

* `migration-check` is still a console script of this install -
  `snap | show | diff | watch`, same options, same exit codes.
* `migration_check` imports nothing from `switch_migrator`. The arrow is
  switch_migrator → migration_check → fabric_weft, as before, and
  `tests/test_mac_versions.py` fails on the first import that breaks it. The
  package can be lifted back out.
* **fabric-weft stays its own repository.** A parser library with its own tests
  and records, plausibly useful to something else, and the only part of the
  toolchain that is. Still installed from `../fabric-engine-parser` or
  `vendor/fabric-weft/`, still with `--no-index`.

**Used through the whole workflow, not just bolted on.** Everything 0012 and D9
wired up is unchanged (`--mac-snapshot`, `--verify-migration --mac-baseline`,
`--mac-watch`, menu option 2 and `v`). Three gaps closed with the move:

* **Menu option `c`, "Compare MAC snapshots"** - the comparison on two folders
  that already exist, no SSH, no collected data, no credentials. It was only ever
  reachable by dropping to a shell for `migration-check diff`; the menu is where
  the field engineer actually is. The cabling sheet is optional there and says
  what is lost without it (a port-exact verdict).
* `MacCheckJob.sheet` is `Sheet | None`, so one glue path serves both the
  collection-driven check and the offline one - the report, its JSON and the
  handover pickup are identical.
* The handover index page now **describes** `mac-check-*.txt/.json`. It had been
  copying them in with no blurb since D9.

**One version, one suite.** `migration_check.__version__` is this repository's,
asserted against `switch_migrator.__version__` and `pyproject.toml`; it is the
`tool` field of every `snapshot.json`, while compatibility stays
`SNAPSHOT_FORMAT`. The tests came in as `tests/test_mac_*.py`, fixtures under
`tests/fixtures/mac-check/`; the ERS capture is read from `tests/fixtures/ers`
here, so the formerly-skipped cross-repo test always runs. **528 tests pass, no
skips** (was 472 here + 56 there, of which one skipped), `ruff check .` and
`mypy` clean.

*Software Engineer:* two `ruff` bars in one repository - `migration_check` kept
the four ignores its own config carried (magic values, many arguments, imports
inside functions, the early-return table in `roles.py`) as `per-file-ignores`,
while D11 made `switch_migrator` split functions instead. **Accepted:** rewriting
moved code during a move buries the move in the diff - the same reason D11 left
`ruff format` alone. The scoped ignores are commented with their reasons and are
for whoever next touches those files.
*Critic:* 0001 said the verification should be usable without the migrator, and
this makes it a thing you get by installing the migrator. **Accepted as the
trade:** the import boundary and the console script are what "usable without"
has to mean in practice, and both are kept and tested. If it ever has to stand
alone again - the TRuffle command 0012 mentions - the test is the guarantee that
lifting the folder back out still works.
*Field Engineer:* nothing to relearn. Same commands, one fewer `git clone`.

## D10 — The cabling sheet is the plan file (2026-11-24)

D2 said "a YAML file the Excel sheet round-trips into". Two edit surfaces for one
plan is one too many, and the field already lives in the sheet. So the plan is
rendered **into** the cabling sheet (NEW switch / port / MLT ID / MLT name, plus a
*Plan note*), the human edits the sheet, and `--pins SHEET` reads those edits
back as pins for the next plan. No YAML.

Consequence: a filled NEW column now means *planned*, not *moved*. The sheet
gets a **Done** column; once anyone ticks it, verification and the MAC check
look at ticked rows only and leave planned rows pending. Untouched, the sheet is
read exactly as before.

Other choices made while building it:
* R4 "balance at random" is implemented as **alternating**, not random: the
  same input must give the same plan, or two runs disagree on the floor.
* The rule for an old SMLT across a vIST pair: both old switches must be in the
  run and recognised as peers (IST peer IP, or the IST MLT's LLDP neighbour).
  One leg alone becomes a plain port, flagged.
* A one-link MLT becomes a plain port, flagged.
* The layout has no default (`plan.ports` / `plan.uplinks` must be given): a
  wrong guess plans servers onto uplink ports.

*Software Engineer:* D7's PortState split (facts / derived / plan) did **not**
happen: the plan lives in its own model (`MigrationPlan`), which was the part
that mattered, and PortState is unchanged. **Recorded:** the split stays on the
list for when the config renderer (phase 3) needs immutable facts.
*Field Engineer:* R6 (downstream switches) is still unanswered; they are
planned like any other device. **Accepted** until someone says otherwise.

## D8 — Frozen

No new work on: console server, profiles (the CLI kind), handover bundle,
run manifest, location split, coverage sheet, progress UI. They keep working
and keep their tests. Unfreezing needs a reason from the field (R20).
