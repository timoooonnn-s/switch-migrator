# 0002 Snapshots are raw output, in switch-migrator's layout

**Status:** accepted, 2026-11-23

**Context.** A pre snapshot is taken once and cannot be taken again after the change. The
parsers will keep improving (fabric-weft is weeks old, the BOSS adapter is new).

**Decision.** A snapshot is a directory of the switches' raw text, one file per command,
in switch-migrator's `--save-raw` layout: `<snapshot>/<device>/<command_slug>.txt`, with
the same slug rule (lower case, every run of other characters becomes `_`). Next to them,
`snapshot.json` records when, by which tool version, which platform each device is, and
which commands failed or were rejected. Parsing happens when the snapshot is read.

A directory without `snapshot.json` (a switch-migrator `raw/` folder) is read too: every
sub-directory is a device, the platform is guessed from the files present or given with
`--platform`.

**Consequences.** An old baseline is re-read with today's parsers. Raw files are switch
output: they hold MACs, IPs and names, so they are work data and never go into this
repository (R8). switch-migrator's VOSS raw folders use `show interfaces gigabitEthernet
fdb-entry`, which this tool does not read yet; its BOSS folders work when they hold
`show mac-address-table` (only sent with `--migration-sheets`).
