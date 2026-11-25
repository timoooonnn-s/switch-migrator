# The Council — switch-migrator

Convened 2026-11-19 to break the tool down against what it is supposed to do.
Same rules of order as in `port-preparer` and `TRuffle`: every seat is brutally
honest, the Critic has a nitpick budget of zero, and a seat that produces more
noise than signal gets retired here with a one-line reason.

## What the tool is supposed to do (the user's brief, verbatim intent)

1. Take the output from the old switch.
2. Prepare the config for the new switch **in company standard**. The new switch
   came through ZTP, so the basic config already exists.
3. Output **aggregated** config snippets for the new switch.
4. Create a **cabling sheet**: for every port, where it is now, all neighbour
   information, and where it will move.
   * A clean solution for hosts the switch **cannot see**, which exist only in
     the cabling documentation (a web tool without an API) and physically on
     the switch.

Everything the tool does is judged against these four. Anything else is
either a precondition for them or scope creep.

## Seats

| Seat | Set by | Mandate |
|---|---|---|
| **Field Engineer** | user | Runs the tool before, during and after a migration and depends on it at 03:00. Judges the workflow, the outputs and what happens when it is wrong. |
| **Software Engineer** | user | Writes the Python. Clean, organised, well structured, every name says what it is. Owns module boundaries and the data model. |
| **Fabric Architect** | added | Owns the *target*: what a finished new switch must look like (company standard, ZTP baseline, UNI model, SMLT/LACP design). Nobody owned this seat so far, and it shows. |
| **Rack Technician** | added | Never runs the tool. Holds the printed cabling sheet in a cold aisle and moves the cables. Judges the paper. |
| **Documentation Steward** | added | Owns the boundary between the switches and the cabling documentation web tool. The "hosts you don't see" problem lives here. |
| **Critic** | added | Challenges claims, scope and process. Speaks only on material issues. |
| **Chair** | Claude | Moderates, writes the synthesis and the questions. Speaks last. |

## Seats considered and not filled

* **Security / Warden** — the tool is read-only, drops secrets from extracts
  and snapshots, and never stores credentials. Worn as a hat by the Critic.
  Promote to a seat the moment the tool scrapes the cabling web tool with a
  login, or writes to a device.
* **Tester** — the suite (407 tests, ~1 s, offline) is in good shape. Worn as
  a hat by the Software Engineer until the plan changes the data model.

## Log

* 2026-11-19 — Council formed. Five seats added next to the two the user set.
  First review: `REVIEW-2026-11-19.md`. Questions: `OPEN-QUESTIONS.md`.
* 2026-11-19 — Round 2 after the user's answers: `DECISIONS.md` (D1–D8) and
  `PLAN.md` (phases 0–4) written. All seats kept: the Fabric Architect drove
  D3, the Documentation Steward D5, the Rack Technician D6, the Critic D8.
* 2026-11-23 — migration-check integrated (D9) and Phase 0 done. The Critic
  caught the read-back trap (a rack sheet with an 'Old port' column would be
  read as a second cabling sheet); the Rack Technician's faceplate order and
  the Fabric Architect's "never invent LACP" both landed as tests.
