# 0008 Dependencies

**Status:** accepted, 2026-11-23

**Decision.** Python ≥ 3.11 (the work server). Runtime: standard library and fabric-weft.
netmiko is an optional extra (`.[capture]`), imported only by `snap`. PyYAML is imported
only when an inventory file ends in `.yaml`/`.yml` (switch-migrator's `switches.yaml`);
CSV inventories need nothing.

**Benefit named.** `diff` and `show` run anywhere a snapshot can be copied to, with no
SSH stack installed.
