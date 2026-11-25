# 0010 Port map: a refinement, not a second verification

**Status:** accepted, 2026-11-23

**Context.** switch-migrator's `--verify-migration` already checks every sheet row: new
port up, an expected MAC on it, LLDP, VLANs, MLT. Its sheet reader is careful (columns by
name, every worksheet, .xlsx and .csv, half-filled rows). Copying either would break R1.

**Decision.** A port map is `(old device, old port) -> (new device, new port)`. It adds
what the per-row check cannot see:

- verdict **`other-port`**: the MAC is back on an access port in the right VLAN, but on
  none of the ports its row names. Two of these on neighbouring ports are swapped cables.
- per row, only the link problems: new port down, new port or old port unknown.

Standalone, the map is a CSV with the sheet's column names ('Old switch', 'Old port',
'New switch', 'New port'), so switch-migrator's `--csv` export reads as it is. The .xlsx
is read only by switch-migrator, which passes the map in (decision 0012). Port names are
normalised with the same rule as switch-migrator ('Port 1/7' -> '1/7').

**Consequences.** Without a map, a swapped cable is `found` (the MAC is back, just not
where planned). Old ports with edge MACs that are not in the map are named in the notes.
