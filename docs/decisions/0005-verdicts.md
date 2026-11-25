# 0005 Four verdicts for a MAC that was there before

**Status:** accepted, 2026-11-23

**Decision.** For every edge `(MAC, VLAN)` of the baseline (VLAN translated through
`--vlan-map`), looked up among *all* sightings after the change:

| Verdict | Meaning | What the technician does |
|---|---|---|
| `found` | on an edge or AP port, same VLAN | nothing |
| `elsewhere` | same VLAN, but only over an uplink or through the fabric | it is plugged in somewhere else: patching |
| `wrong-vlan` | seen, but only in another VLAN | port config, or NAC put it in another VLAN |
| `missing` | seen nowhere | cable, port down, PoE, or the device is just quiet |

Edge MACs after the change that were not edge MACs before are listed as `new`.

The baseline can be several pre snapshots: their edge sightings are merged, the latest
sighting of a `(MAC, VLAN)` wins. This catches devices that were quiet in one of them.

**Consequences.** Exit code 1 whenever anything is missing, elsewhere or in the wrong
VLAN, or a VLAN with edge MACs before exists on no device after.
