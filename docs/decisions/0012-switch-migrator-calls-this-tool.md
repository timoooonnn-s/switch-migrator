# 0012 switch-migrator calls this tool

**Status:** accepted, 2026-11-23

**Decision.** `switch-migrator --verify-migration SHEET --mac-baseline DIR` (repeatable):

1. checks the options before any switch is touched (migration-check installed, the
   folders exist, not with `--from-snapshot`);
2. after its own verification, reads the new switches named on the sheet through its own
   runners (`make_runner`, so legacy SSH, console server and `--offline` all apply) with
   this tool's command list, into `<output>/mac-check-<stamp>/after/`;
3. compares with the baseline(s) using the sheet's migrated rows as the port map;
4. writes `mac-check-<stamp>.txt` and `.json` with its other reports (handover and
   manifest pick them up) and prints one summary line.

The glue is `switch_migrator/mac_check.py`, about 100 lines. switch-migrator imports this
tool; this tool never imports switch-migrator.

**Exit code.** Unchanged by MACs not back (switch-migrator treats "no MAC yet" as a warning).
Set to 1 when the MAC check could not run.

**Not done.** The interactive menu; old VOSS baselines (switch-migrator saves
`show interfaces gigabitEthernet fdb-entry`, which this tool does not read); one SSH
session per new switch is opened a second time for the extra commands.
