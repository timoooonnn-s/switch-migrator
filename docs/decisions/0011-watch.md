# 0011 Watch until the result settles

**Status:** accepted, 2026-11-23

**Context.** The first after snapshot always over-reports (quiet devices). The advice
"run it again in a few minutes" was manual.

**Decision.** `migration-check watch OUT` takes an after snapshot every `--interval`
seconds into `OUT/round-NN`, compares each with the same baseline, and prints one trend
line per round (not found, by verdict, and how many came back since the last round). It
stops when everything is found, when the number not found has not fallen for `--stable`
rounds, or after `--rounds`. The last round's full report is printed. Every round is a
normal snapshot, so any of them can be compared again later.

Snapshot and comparison are passed into `watch()` as functions: the tests need no switch,
and switch-migrator can drive it with its own sessions later.

**Defaults.** 120 s, 15 rounds, stop after 3 rounds without progress: half an hour at
most, which covers the ~5 min MAC ageing several times over.
