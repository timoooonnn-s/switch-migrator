# 0004 Only edge MACs are compared

**Status:** accepted, 2026-11-23

**Context.** An access switch learns the gateway, servers and every MAC in the VLAN's
flooding domain on its uplink. Counted as "before", all of them look missing once the old
switch is gone. Wireless clients roam and sleep.

**Decision.** Every MAC sighting gets a role:

| Role | When | Compared |
|---|---|---|
| `edge` | learned on a front port (or an MLT) with no switch or router behind it | yes |
| `access-point` | learned on a port whose LLDP neighbour is a WLAN access point, and not the AP's own MAC | no, counted |
| `uplink` | the port or an MLT member has an LLDP neighbour that is a bridge or router (and not a phone or AP), or the user said so with `--uplink` | no |
| `fabric` | learned through the SPB fabric from another BEB (Fabric Engine `TUNNEL`) | no |
| `switch` | the switch's own MAC | no |
| `unknown` | the interface could not be resolved to a port (e.g. an MLT name not in `show mlt`) | no, reported |

LLDP capabilities are read from the *enabled* half of `SysCap` (`rB / rB`, `Br / Br`).
`--edge DEVICE:PORT` overrides a port wrongly taken as an uplink (a hypervisor that
advertises Bridge, for example).

**Consequences.** Every report states how many MACs fell into each role per device, so a
wrong classification shows up as a strange number rather than silently.
