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

## D9 — migration-check is the MAC-level verification (2026-11-23)

`pre-migration-checker` (`migration-check`) stays a separate package; the
arrow is switch-migrator → migration-check → fabric-weft, never back (its own
decisions 0001/0012). It is an **optional** dependency: without it everything
else works and the MAC options say how to install it. It is not declared in
`pyproject.toml`, because it is not on PyPI and a same-named package there
would be installed instead (dependency confusion).

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
