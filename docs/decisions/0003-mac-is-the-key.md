# 0003 The MAC is the key, the port is information

**Status:** accepted, 2026-11-23

**Context.** A migration renames every port (BOSS `7` or `2/7` becomes FE `1/41`) and
often consolidates several old switches onto fewer new ones.

**Decision.** MACs are compared by `(MAC, VLAN)` across the whole scope (all devices of a
snapshot), never device by device or port by port. The old device and port are carried
along to tell the technician where to look. LLDP neighbours are keyed by what they
advertise (chassis ID, else system name, plus remote port), not by the local port.
Active ports are compared as counts per device until a port map exists.

**Consequences.** Works the same for a 1:1 swap, a consolidation and a firmware upgrade.
A VLAN renumbered on purpose is declared with `--vlan-map OLD=NEW`.
