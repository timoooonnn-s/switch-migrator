# 0009 How a switch is reached is pluggable

**Status:** accepted, 2026-11-23

**Context.** `snap` v0.1 opened plain netmiko sessions. switch-migrator's `SshRunner`
does much more for the same switches: the legacy SSH algorithms old ERS/BOSS gear needs
(ssh-dss host keys, SHA-1 key exchange), console-server access for switches without a
management IP, paging, giving up after repeated transport failures, offline replay.

**Decision.** `capture()` takes a `connect(target) -> Session`, where a session has
`run(command) -> text` and `close()`. `run` returns the switch's answer, an error answer
included (the command is then recorded as rejected); it raises only when no answer came.
`netmiko_sessions()` is the built-in way; switch-migrator passes its own runners. The
capture extra pins `paramiko<4`, as switch-migrator does, because 4.x dropped ssh-dss.

**Consequences.** Plain `migration-check snap` may still fail on very old BOSS where
switch-migrator succeeds (it also appends legacy algorithms to paramiko). For such
switches, take the baseline with switch-migrator (`--migration-sheets --save-raw`).
