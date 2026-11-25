# 0006 Platforms

**Status:** accepted, 2026-11-23

**Decision.** Two platforms, each an adapter that turns a device's raw output into the
OS-neutral records of `model.py`:

- **Fabric Engine / VOSS** reads through fabric-weft: `show vlan mac-address-entry`,
  `show lldp neighbor`, `show mlt`, `show vlan basic`, `show interfaces gigabitEthernet
  state` and `name`, `show ip arp`, `show interfaces gigabitEthernet i-sid`.
- **BOSS** (ERS lineage) has its own small parsers in `platforms/boss.py`:
  `show mac-address-table`, `show lldp neighbor`, `show mlt`, `show vlan`,
  `show interfaces`. They live here, not in fabric-weft (a different OS family), and not
  in a new library until a second tool needs them.

`voss`/`ers` are accepted as names for the two, as in switch-migrator's inventory.
EXOS / Switch Engine is out of scope *(user)*.

**Evidence.** BOSS shapes come from switch-migrator's `tests/fixtures/ers/`, from work
*(user)*. `show mac-address-table` has only the three-line sample in switch-migrator's
`tests/test_migration_sheets.py`. Stack port names (`1/7`) and the two-entries-per-line
layout of older releases are handled but unseen.
