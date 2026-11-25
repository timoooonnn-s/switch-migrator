# migration-check — the MAC check on its own

**Snapshot the switches before a change and after it, then compare by what matters:**
which end-device MACs, LLDP neighbours, VLANs and active ports are missing or new.

```
7 of 212 edge MACs not seen after the change
  missing         5  seen nowhere
  wrong VLAN      1  seen, but only in another VLAN
  elsewhere       1  same VLAN, but only over an uplink or through the fabric
  found         205  on an access port, same VLAN

MISSING (5), by old port
  old-access-01 port 7: 2 MAC(s) (LLDP SEP001122334455)
      00:11:22:00:00:11  VLAN 735   10.7.35.10
```

Read-only: only `show` commands are sent. BOSS (ERS) and Fabric Engine / VOSS.

This was its own repository, `pre-migration-checker`. It is now the
[`migration_check/`](../migration_check) package of switch-migrator
([D14](../council/DECISIONS.md), [decision 0013](decisions/0013-one-repository.md)) and
still runs on its own: `migration-check` is a console script of the same install, and the
package imports nothing from `switch_migrator`. Its own decision records — why the MAC is
the key, which MACs are compared, how verdicts are decided — are in
[`docs/decisions/`](decisions).

Nothing to install separately: follow switch-migrator's
[Installation](../README.md#installation) and you have both commands.

```bash
.venv/bin/migration-check --version
```

## Use

```bash
# the day before, and again right before the window: two baselines are better than one
migration-check snap snapshots/pre-day-before --label pre --inventory switches.yaml
migration-check snap snapshots/pre --label pre --device 10.1.1.10=old-access-01 --platform boss
migration-check show snapshots/pre          # is the baseline complete? MACs per role per switch

# after the change
migration-check snap snapshots/post --label post --device 10.2.0.41=leaf-01 --platform fe
migration-check diff --before snapshots/pre-day-before --before snapshots/pre \
                     --after snapshots/post --json result.json
```

- `--inventory` takes switch-migrator's `switches.yaml` or a CSV (`name,host,platform`).
- The password is asked for, or read from `MIGRATION_CHECK_PASSWORD`; the user name from
  `--username`, `MIGRATION_CHECK_USERNAME` or your login. Neither is ever written.
- `diff` also reads switch-migrator `--save-raw` folders (same layout). For a folder
  whose files do not tell the platform: `--assume-platform boss|fe`.
- Exit codes: `0` clean, `1` something to look at, `2` could not run.

**A device that has not sent a frame since the change is in no MAC table yet** and shows
as missing until it does. So instead of one `diff`, let `watch` take the after snapshots:

```bash
migration-check watch snapshots/post --before snapshots/pre --device 10.2.0.41=leaf-01 \
                --platform fe --interval 120 --rounds 15 --stable 3
# round  1  23:41:05  not found   31 of 212  (missing 27, wrong VLAN 1, elsewhere 3)
# round  2  23:43:09  not found   12 of 212  (missing 8, wrong VLAN 1, elsewhere 3)  back since last round 19
# ...
# stopped after round 5: the number not found stopped falling
```

Each round is a normal snapshot (`snapshots/post/round-NN`); the last round's full
report is printed.

### With the cabling sheet: `--port-map`

```bash
migration-check diff --before snapshots/pre --after snapshots/post --port-map Cabling.csv
```

A CSV with the columns 'Old switch', 'Old port', 'New switch', 'New port'
(switch-migrator's `--csv` export of the Cabling sheet works as it is). It adds the
verdict **other port**: the MAC is back on an access port, but not on the one its row
says. Two of those next to each other are swapped cables. Rows whose new port is down or
unknown are listed. The .xlsx sheet is read by switch-migrator, not here.

## From switch-migrator

Three ways in, all documented under
[Every MAC, across all switches](../README.md#every-mac-across-all-switches---mac-snapshot----mac-baseline):

* `switch-migrator --mac-snapshot DIR` takes a **baseline** on the sessions its own
  collection already has open — no second login, and everything switch-migrator can
  reach (legacy SSH for old ERS, the console server, `--offline` replay) applies. For
  very old BOSS gear, take the baseline this way: plain netmiko may not connect where
  switch-migrator does (decision 0009).
* `switch-migrator --verify-migration cabling.xlsx --mac-baseline DIR` runs this
  comparison as part of its verification, reading the new switches itself, using the
  .xlsx sheet as the port map, and writing `mac-check-<timestamp>.txt/.json` with its
  other reports (decision 0012). Add `--mac-watch` for the `watch` behaviour above.
* The menu's **`c`, Compare MAC snapshots** is `diff` without leaving the tool: two
  folders that already exist, no SSH, no collected data, no credentials.

## How it decides

- **The MAC is the key, not the port** (decision 0003): BOSS `7` becomes FE `1/41`, three
  old stacks become two new switches. All devices of a snapshot are one scope.
- **Only edge MACs are compared** (0004). MACs learned over uplinks, through the fabric,
  or behind a WLAN AP are counted per device but not compared. LLDP decides: a neighbour
  advertising Bridge or Router (and not Telephone or WLAN) makes a port an uplink.
  Override with `--uplink DEVICE:PORT` / `--edge DEVICE:PORT`.
- **Verdicts** (0005, 0010): found · other port (only with a port map) · elsewhere (same
  VLAN, but over an uplink / via another BEB: patched somewhere else) · wrong VLAN (port
  config, or NAC) · missing. `--vlan-map OLD=NEW` for VLANs renumbered on purpose.
- **Lenient and loud** (0007): an unread line, a rejected command or a missing file is a
  problem printed at the top and makes the exit code 1.

## What it cannot see (printed in every report)

- quiet devices (above) · MACs behind ELAN / Switched UNI endpoints on Fabric Engine ·
  devices that fail NAC · anything that is not in a MAC table (power, link without
  traffic).

## Development

One suite for both packages, from the repository root:

```bash
pytest && ruff check . && mypy
```

The MAC check's tests are `tests/test_mac_*.py`, its fixtures
`tests/fixtures/mac-check/` (with a README saying where each capture came from) and
`tests/fixtures/ers/`. `tests/test_mac_check.py` covers the glue on switch-migrator's
side, `switch_migrator/mac_check.py`. **Snapshots are work data and never go into this
repository** (`.gitignore` blocks them).
