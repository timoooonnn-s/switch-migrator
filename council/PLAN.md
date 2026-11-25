# Plan

Owner: the Chair. The single source of truth for what happens next.
Revised 2026-11-19 after round 1 answers. Decisions referenced as D1–D8
(`DECISIONS.md`), questions as R1–R20 (`OPEN-QUESTIONS.md`).

## Target workflow

```
collect ──▶ facts ──────────────▶ plan ─────────────▶ render ──────────────▶ verify
 old switches    switch facts        allocator (D2)       per NEW switch:          against the plan
 new switches    + AixBOMS import    + human pins         I-SIDs, ports, MLTs (D3) + AixBOMS changes (D5)
 DvR fabric      + reconciliation    + validation         rack sheet, engineer
                   (D5)                                   sheet (D6), verify cmds
```

Three verbs for the field engineer: **collect**, **plan**, **verify**.
`render` runs as part of `plan` and on demand.

## Phases

Each phase ends with something usable on a real migration, tests green.

### Done 2026-11-23 — migration-check integrated (D9)

`--mac-snapshot` (baseline on the collection's own sessions), `--mac-baseline`
on the verification, `--mac-watch`, menu options 2 and `v`, dry-run, README.

### Phase 0 — fix what is wrong today — **done 2026-11-23**

All six items below are in, each with a test that fails on the code before it
(8 new tests; 433 pass). Found on the way, now a follow-up: **ERS LACP state is
never read**, so on ERS→FE every MLT comes out "LACP unknown" (honest, but
manual). Needs a real ERS capture with an LACP trunk (C3) before it is parsed.

Small, separate commits on the existing code:

1. `--generate-mlt`: stop switching LACP on for every MLT — carry the old
   LACP state (add it to the sheet), never invent it.
2. `--generate-mlt`: a single-member SMLT is the standard server design, not a
   problem — no warning when the MLT is an SMLT.
3. Cabling sheet: sort ports numerically (faceplate order), not as strings.
4. Configured port names on the sheets: VOSS `name`, ERS `name port`.
5. Rename `PortState.description` → `media`.
6. Add the AixBOMS-free half of the rack sheet (D6) so the techs get a
   printable sheet now.

**Done when:** each fix has a test that failed before it.

### Phase 1 — the migration plan — **done 2026-11-24** (D2, D10)

`--plan` / menu option 5: allocator, validator (double-booked, uplink port,
partly pinned, overflow), plan sheets, pins from an edited sheet, Done column,
rack sheet listing unused ports. Not done from the list below: step 1 (the
PortState split, see D10) and a YAML plan file (replaced by the sheet, D10).
**Done when** still needs its real test: one real old switch (C3).

1. Split `PortState` into facts / derived / plan (D7).
2. `model/plan.py`: `MigrationPlan`, `Attachment` (dual-/single-homed /
   infrastructure), `PortMove`, with provenance per decision.
3. The allocator (D2) with the port layout of each new model (R1).
4. The validator: double-booked port, SMLT leg on one peer, reserved port
   used, pair full.
5. Plan file (YAML) + read-back from the Excel sheet; pins respected.
6. `workflow/collect.py` and `workflow/plan.py`; CLI and menu both call them.

**Done when:** one real old switch (C3) produces a plan you would accept
with fewer than a handful of manual changes.

### Phase 2 — AixBOMS (needs R13–R17, C5)

1. Importer (D5): xlsx + csv, value classification, side detection, TX/RX
   merge, unplaced rows.
2. Reconciliation; doc-only links become plan attachments.
3. Rack sheet gains patch cable numbers and AixBOMS names.

**Done when:** C3 + C5 of the same switch reconcile, and every row of the
export is either placed or listed as unplaced.

### Phase 3 — config per new switch — **built 2026-11-25** (D3)

`config/new/<switch>.cfg` from every `--plan`. Still open: the baseline check
against a ZTP'd switch (C1), golden-file tests from a finished switch (C2) -
which also settles the three [UNVERIFIED] line kinds - and the platform-VLAN
form (R9).

### Phase 3 — config per new switch (needs R9–R12, C1, C2) — original list

1. Conventions and access-port profile from `port-preparer` (D4).
2. Renderer: I-SID services, port phases I/II, MLT/SMLT (D3).
3. Baseline check against C1.
4. Golden-file tests from C2.

**Done when:** the rendered file for one new switch matches what you would
have typed, line for line, apart from documented differences.

### Phase 4 — verify against the plan

1. `--verify-migration` reads the plan, not only the sheet.
2. AixBOMS changes list (D5).

Phases 2 and 3 can swap, depending on which input arrives first: the AixBOMS
export or the gold-standard config.

## Not now

Office/branch (D1). Anything in D8. Writing to devices.
