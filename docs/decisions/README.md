# migration_check decisions

The decision records of the MAC check, written while it was its own repository
(`pre-migration-checker`). They moved here with the code in 0013 and are kept as they
were: they are the reason this package looks the way it does. Decisions about
**switch-migrator as a whole** live in [`council/DECISIONS.md`](../../council/DECISIONS.md).

One file per decision, never rewritten (rule R6). A decision that changes gets a new
record that says which one it supersedes.

| # | Decision |
|---|---|
| 0001 | Name and placement: a standalone tool in its own repository (placement superseded by 0013; its dependency rule stands) |
| 0002 | Snapshots are raw output, in switch-migrator's layout |
| 0003 | The MAC is the key, the port is information |
| 0004 | Only edge MACs are compared |
| 0005 | Four verdicts for a MAC that was there before |
| 0006 | Platforms: Fabric Engine through fabric-weft, BOSS through an adapter here |
| 0007 | Lenient parsing, loud about it; blind spots in every report |
| 0008 | Dependencies |
| 0009 | How a switch is reached is pluggable |
| 0010 | Port map: a refinement, not a second verification |
| 0011 | Watch until the result settles |
| 0012 | switch-migrator calls this tool |
| 0013 | One repository: this package ships inside switch-migrator (supersedes 0001's placement) |
