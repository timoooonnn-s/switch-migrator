# 0001 Name and placement

**Status:** accepted, 2026-11-23

**Context.** switch-migrator has a sheet-driven `--verify-migration`. The user wants the
verification usable without the migrator, kept in this repository and "as compatible as
possible" with switch-migrator, which may itself change shape.

**Decision.** A standalone package, `migration_check`, with its own command
`migration-check`. It imports nothing from switch-migrator. The dependency arrow is
switch-migrator → migration-check → fabric-weft, never the other way.

**Consequences.** switch-migrator can later call `migration_check.compare` and pass its
cabling sheet as an optional port map. The name is a working name; renaming the package
is cheap while nothing imports it.
