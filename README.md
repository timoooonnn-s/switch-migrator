# switch-migrator

Read-only **pre-migration audit tool** for Extreme Networks switches.

Given a list of legacy switches (VOSS / Fabric Engine 8.x–9.4.x and ERS/BOSS
stackables), the tool:

1. **Audits the legacy switches** over SSH:
   * which ports are operationally up (incl. LLDP neighbor names),
   * which of those are **uplinks** to your predefined core switches,
   * MLT/SMLT state incl. per-member link state,
   * **IST / vIST** state (peer, VLAN, session status),
   * all VLANs, and on VOSS the local VLAN ↔ I-SID bindings.
2. **Reads the authoritative fabric state from the DvR controllers**:
   the domain-wide DvR interfaces with their L2 I-SID ↔ VLAN pairs
   (`show dvr interfaces`), which I-SIDs exist fabric-wide
   (`show isis spbm i-sid all`), plus the controllers' local c-vid
   attachments (`show i-sid`, `show vlan i-sid`) and the BEBs services
   terminate on.
3. **Compares** every VLAN on every to-be-migrated switch against the fabric
   state and flags anything that would break during migration.

The tool **only ever sends `show` commands**. It never changes device
configuration, making it safe to run against production at any time.

## How the VLAN → I-SID matching works

Your I-SIDs follow an offset convention with several possible prefixes
(candidate I-SID = *offset* + *VLAN-ID*), so the tool checks **both**:

* the **convention**: every configured offset produces a candidate I-SID
  (per-VLAN explicit overrides win), and
* the **actual DvR state**: which I-SID(s) the DvR controllers show that VLAN attached to.

The DvR state is authoritative. Match results per VLAN:

| Status | Meaning | Severity |
|---|---|---|
| `OK` | Convention I-SID exists in the fabric **and** the DvR controllers confirm the VLAN attachment (or the VOSS local binding matches). | ok |
| `OK_NONSTANDARD` | The DvR controllers attach the VLAN to an I-SID that does **not** match any configured offset. Service exists, but document the exception. | warn |
| `IN_FABRIC_NOT_ATTACHED` | A convention I-SID exists in the fabric, but no DvR controller shows this VLAN attached. Usually means the c-vid terminates on other BEBs — verify before migrating. | warn |
| `AMBIGUOUS` | Multiple candidate I-SIDs match. Resolve with an `isid_conventions.explicit` entry. | error |
| `MISSING_ON_DVR` | No candidate I-SID exists anywhere in the fabric. The L2VSN must be created before this switch can be migrated. | error |
| `LOCAL_ISID_NOT_IN_FABRIC` | A VOSS switch binds the VLAN to an I-SID that no DvR controller knows. Broken/orphaned service. | error |
| `LOCAL_BINDING_CONFLICT` | A VOSS switch binds the VLAN to one I-SID, but the DvR controllers attach that VLAN to a *different* I-SID. Resolve before migrating. | error |
| `EXCLUDED` | VLAN matches `excluded_vlans` (by id) or `excluded_vlan_names` (by name glob, e.g. a quarantine VLAN). The row stays in the report so you can see on which switches it exists, but it never counts as an error. | – |

## Requirements

* Python **3.10+** on a Linux host with direct SSH reachability to all devices
* Legacy switches: Extreme **VOSS / Fabric Engine** (tested formats 8.x–9.4.x)
  and **ERS / BOSS** stackables
* DvR controllers: VOSS / Fabric Engine
* An account with permission to run `show` commands (read-only account is
  sufficient and recommended)

## Installation

```bash
git clone <this repo>
cd switch-migrator
python3 -m venv .venv && source .venv/bin/activate
pip install .            # or: pip install -r requirements.txt
switch-migrator --version
```

## Configuration

```bash
cp config.example.yaml config.yaml       # DvR controllers, conventions, patterns
cp switches.example.yaml switches.yaml   # the switches to migrate
```

Both real files are gitignored — never commit device data.

Key settings in `config.yaml` (see the example file for full comments):

| Key | Purpose |
|---|---|
| `dvr_controllers` | The DvR controllers to read the fabric state from. Views of all DvR controllers are merged. |
| `core_switch_patterns` | Case-insensitive globs matched against LLDP `SysName`; matching neighbors mark a port/MLT as **uplink**. |
| `isid_conventions.offsets` | All offsets in use (`I-SID = offset + VLAN`). |
| `isid_conventions.explicit` | Per-VLAN overrides, e.g. `150: 90150`. |
| `excluded_vlans` | VLANs to skip (default 1, B-VLANs 4048–4059, IST VLAN, …). |
| `ssh` | Timeouts, retries, parallel workers. |

## Credentials

Credentials are **never** stored in config files. Resolution order:

1. Environment variables `SM_USERNAME` / `SM_PASSWORD`
   (and optionally `SM_DVR_USERNAME` / `SM_DVR_PASSWORD` if the DvR controllers use a
   different account — otherwise the switch credentials are reused),
2. interactive prompt.

```bash
export SM_USERNAME=readonly
read -s SM_PASSWORD && export SM_PASSWORD
```

Authentication failures are **not retried** to avoid account lockouts.

## Legacy SSH on old ERS gear

Old ERS/BOSS switches only offer SHA-1 key exchange, CBC ciphers and
`ssh-rsa`/`ssh-dss` host keys. The tool handles this out of the box
(`ssh.legacy_algorithms: true`, the default):

* the legacy algorithms are **appended** to paramiko's client preference
  lists at runtime — modern algorithms keep priority, so VOSS switches and
  DvR controllers negotiate exactly what they would anyway; only servers
  that offer nothing better fall back to the legacy set,
* `requirements.txt` pins `paramiko>=3.4,<4` because paramiko 4.x removed
  `ssh-dss` host-key support, which the oldest ERS boxes still present.

Everything happens inside the tool's own Python environment (your venv's
paramiko does its own crypto — RHEL system-wide crypto policies and the
OpenSSH client config don't apply to it). No root access, no OS changes, no
device changes and no multi-user settings are needed; a plain user account
on a RHEL 8/9 host is enough.

A device that still can't connect is reported as **UNREACHABLE** (console +
Summary + Issues) and the audit simply continues with the remaining
switches — one dead box never aborts the run.

## Usage

### Interactive menu (default)

Run it with no arguments and you land in the toolkit menu:

```bash
switch-migrator                      # or: switch-migrator --menu
switch-migrator -c config.yaml -i switches.yaml     # menu, pre-loaded
```

```
╭─ switch-migrator ───────────────────────────────╮
│ Config:    config.yaml                          │
│ Inventory: switches.yaml                        │
│ Switches:  12 selected of 12                    │
│ Output:    output                               │
│ Data:      collected 14:03:11 (12 switch(es),   │
│            incl. fabric, running-config, MACs)  │
╰─────────────────────────────────────────────────╯
  1  Select switches       pick targets from the inventory or add them by hand
  2  Collect from devices  connect once; all outputs below reuse this data
  3  Audit vs fabric       compare every VLAN against the DvR fabric state
  4  Inventory report      port/MLT/VLAN state only, no fabric comparison
  5  Migration sheets      port info + DC cabling sheet + MAC-check commands
  6  Config extract        neutralized VOSS config / generated ERS->VOSS draft
  7  Everything            run 3-6 in one go with the collected data
  8  Settings              output directory, offline replay, target switch name
  0  Quit
```

The menu holds a **session**: devices are collected **once** (option 2, which
asks whether to include fabric state, running-config and MAC tables), and every
output afterwards is produced from that same data — switching between use cases
costs no further SSH round-trips. Options that need data you didn't collect say
so instead of silently producing empty columns.

**All command-line flags below keep working unchanged** — the menu is an extra
entry point, not a replacement, so existing scripts and cron jobs are unaffected.

### Command line

```bash
# Standard run: inventory file + Excel report + console summary
switch-migrator -c config.yaml -i switches.yaml

# Quick check of two switches without an inventory file
switch-migrator -s old-access-01:ers -s old-agg-01:voss:10.1.1.20

# Everything, incl. CSV export and raw CLI capture
switch-migrator -i switches.yaml --csv --save-raw -v

# Re-run the analysis later without touching any device
switch-migrator -i switches.yaml --offline output/raw

# Inventory-only report for an ISOLATED environment (no fabric to compare to):
# reports each switch's port/MLT/IST/VLAN state - incl. per-VLAN port members -
# and skips DvR collection and comparison entirely. dvr_controllers and
# isid_conventions are optional in the config for this mode.
switch-migrator -c config.yaml -i isolated.yaml --no-fabric

# Also pull each VOSS switch's running-config and write a neutralized,
# migration-ready extract (port/MLT/VLAN/I-SID only) to <output>/config/
switch-migrator -i switches.yaml --extract-config

# Migration-day deliverables: port info sheet + DC cabling sheet + commands
switch-migrator -i switches.yaml --migration-sheets --new-switch new-sw-01 \
                --extract-config
```

### Migration sheets (`--migration-sheets`)

Adds the two worksheets you take into the migration window, plus a commands
file. Every port gets a **sequential migration ID** (`P0001`, …) assigned across
the whole run — the key that ties both sheets together when several old
switches consolidate onto fewer new ones.

* **Port Info** (all ports) — Port ID, switch, port, device on the port (LLDP
  name / IP / SysDescr), MAC addresses, tagging, VLAN IDs, I-SIDs, admin & oper
  state, LACP, MLT ID & name, transceiver, media, uplink flag.
* **Cabling** (connected ports only — what actually gets re-patched) —
  deliberately wide, *one big paper*, so every row is self-contained at the
  rack: first VLAN, type (access/mlt/uplink), Port ID, end device / neighbor,
  **empty NEW switch + NEW port columns for the technicians to fill in**, old
  switch, old port, **MLT ID, MLT name, the MLT's VLANs and I-SIDs**, the
  **port's own VLANs and I-SIDs**, MAC addresses and physical media.
* **`migration-commands-<stamp>.txt`** — per-port
  `show interfaces gigabitEthernet fdb-entry` commands to run on the **new**
  switch (each annotated with the Port ID, the
  old switch/port and the MACs to expect), a post-migration state overview, and
  — when combined with `--extract-config` — the neutralized device config ready
  to copy.

MAC addresses are capped at 10 per port with a `(+N more)` note. Pass
`--new-switch NAME` to name the target device in the commands file.

VOSS has **no** `show mac-address-table`; the forwarding database is read with
`show interfaces gigabitEthernet fdb-entry`. The bare form (whole box, one
command) is tried first, and releases that insist on a port argument fall back
to querying only the ports that are operationally **up**. ERS/BOSS uses the
classic `show mac-address-table`.

### Config extraction (`--extract-config`, VOSS)

Pulls each VOSS switch's `show running-config` and writes a **neutralized,
migration-ready extract** to `<output>/config/<device>.cfg` for review before
you build the new device. It keeps the **port / MLT / VLAN / I-SID** banner
sections verbatim and drops everything else, so:

* **neutralized by construction** — device identity and every secret (mgmt/OOB,
  SNMP, RADIUS/TACACS, SSH/cert, syslog/NTP, boot flags, and the SPB/IS-IS core
  identity: nick-name, system-id, manual-area) live in sections that are never
  emitted, so nothing sensitive can leak;
* **model-agnostic** — keys on the config's own section banners, so a flex-UNI
  leaf, a traditional `vlan i-sid` BEB, a BCB (almost nothing to keep) or an
  isolated VLAN-only box all work;
* **annotated, not paste-ready** — fabric uplink ports (those running IS-IS) get
  a `# [REVIEW]` marker, and the header lists the sections that were present but
  omitted so you remember to configure them separately.

It works with `--offline`/`--save-raw` like everything else.

**ERS → VOSS (generated draft).** For an **ERS** switch, `--extract-config`
instead *generates* a best-effort VOSS **flex-UNI** config from the ERS L2 model
and writes two files:

* `<device>.cfg` — each VLAN becomes an `i-sid <isid> elan` with
  `c-vid <vlan> port …` (tagged members) and `untagged-traffic port …` (PVID
  members); access ports get the flex-UNI boilerplate; old port numbers are kept
  as `1/N`. It is clearly labelled a **DRAFT** with a "YOU MUST VERIFY" header
  (tagged-vs-untagged, uplink/MLT handling, and the untranslated config).
* `<device>.isid-decisions.txt` — the **I-SID decision worksheet**: because
  several offsets run in parallel (`2500000/2510000/2700000/2710000`),
  `offset + VLAN` is ambiguous, so any VLAN the fabric didn't confirm is listed
  with all candidate I-SIDs plus a ready-to-paste `isid_conventions.explicit`
  snippet. Fill in your choices, re-run, and those VLANs resolve. Until then the
  service block is emitted **commented-out** with a `# [REVIEW]` marker — never a
  guessed I-SID.

I-SID resolution order is: excluded → your explicit decision → fabric-confirmed
(from the audit) → REVIEW placeholder.

### Inventory mode (`--no-fabric`)

For isolated environments whose VLANs and I-SIDs are intentionally **not** in a
fabric, `--no-fabric` turns the tool into a pure state report: it collects
ports, MLTs, IST/vIST and VLANs (with each VLAN's configured **member ports**)
from every switch and renders **Summary**, **VLANs**, **Ports**, **MLTs** and
**Issues** — no DvR controllers are contacted and the *VLAN vs Fabric* and
*Fabric I-SIDs* tables are omitted. No `dvr_controllers` or `isid_conventions`
are required in the config.

Output goes to `./output/` by default:

* `migration-audit-<timestamp>.xlsx` — Excel workbook with the sheets
  **Summary**, **VLAN vs Fabric**, **Ports**, **MLTs**, **Fabric I-SIDs**,
  **Issues** (color-coded, filterable, frozen header row),
* `csv-<timestamp>/*.csv` with `--csv`,
* `raw/<device>/<command>.txt` with `--save-raw`,
* `switch-migrator.log`.

**Exit codes** (scriptable): `0` = clean, `1` = errors found (unreachable
device, DvR read failure, or any red comparison result), `2` = config error.

### Reading the report

* **Summary** — one line per switch: ports up/total, **uplinks up/total**,
  MLT count and MLTs with down members, IST/vIST session state, VLAN
  comparison counters (ok/warn/error).
* **VLAN vs Fabric** — the core sheet: per VLAN the expected convention
  I-SID(s), what the DvR controllers actually attach, the matched I-SID and the status
  from the table above. This is your migration checklist.
* **Issues** — flat, filterable list of everything that needs a human:
  unreachable devices, down IST sessions, MLTs with down members, VLAN
  mismatches.

## Commands sent to the devices

| Platform | Commands (read-only) |
|---|---|
| VOSS (migrate) | `enable` (both VOSS and ERS log in at user-EXEC `>`, where `show interfaces`/`show lldp` don't exist — the tool enters privileged EXEC first), `show interfaces gigabitEthernet state` → `show interfaces gigabitEthernet interface` (fallback chain, first that answers wins), `show mlt`, `show virtual-ist`, `show vlan i-sid`, `show vlan basic`, `show vlan members`, `show interfaces gigabitEthernet i-sid`, `show lldp neighbor summary` → `show lldp neighbor`, and `show running-config` (only with `--extract-config`) |
| ERS (migrate) | `enable`, `show interfaces`, `show mlt`, `show ist`, `show vlan` (incl. `Port Members`), `show lldp neighbor` → `show lldp neighbor summary` |
| DvR controller (VOSS) | `show dvr interfaces`, `show isis spbm i-sid all`, `show i-sid`, `show vlan i-sid` |

`show vlan members` (VOSS) and the `Port Members` line of `show vlan` (ERS)
feed the per-VLAN member-port column of the inventory report. Both are optional
and silently skipped on releases that don't support them.

Which command variants a given 8.x release accepts varies (real captures show
boxes rejecting the plain interfaces form or the block-style LLDP command
while accepting the alternatives) — hence the fallback chains. A DvR
controller only counts as an **authoritative** fabric source when the
fabric-wide `show isis spbm i-sid all` succeeded; a controller that only
delivered its local attachments is merged but flagged as partial, so it can
never cause false `MISSING_ON_DVR` verdicts on its own.

Notes on syntax (checked against the Extreme VOSS/Fabric Engine command
references and real device output):

* Port state comes from `show interfaces gigabitEthernet state` — a compact
  table that includes the down `REASON` column (shown in the Ports sheet). On
  releases without the `state` subcommand the tool falls back to plain
  `show interfaces gigabitEthernet` and parses its leading **Port Interface**
  section.
* Plain `show mlt` prints **four** tables (Mlt Info, LACP, local/remote port
  members, ENCAP) plus `All N out of M ...` footers, and the trailing VLAN IDS
  column wraps onto continuation lines for long VLAN lists. The parser accepts
  only genuine Mlt Info rows (they carry a type/state token) and keeps one
  entry per MLT id, so the extra tables, footers and continuation lines can't
  produce phantom or duplicate MLTs.
* **Paging is disabled explicitly and verified** right after login — VOSS:
  `terminal more disable`, ERS: `terminal length 0`. netmiko sends these too
  but never checks the device's answer; if the pager stayed active, every
  long output (Port Interface/State on a 50-port box) would stall at
  `--More--` and the stuck pager would swallow the *next* command. If the
  device rejects the paging command, that appears as a per-switch warning,
  and after any command timeout a `q` is sent to kill a possible stuck pager
  before the next command. No other session-tuning commands are sent (VOSS
  has no `terminal width`; unknown commands desync the channel).
* **Dead MLTs** (no member ports left) are flagged explicitly in the MLTs
  sheet and as a warning: they don't need to be recreated on the new switch.
* `show interfaces gigabitEthernet i-sid` supplements `show vlan i-sid` with
  port-level bindings, catching CVLAN/switched-UNI services; conflicting
  bindings between the two sources are flagged as warnings.
* `show dvr interfaces` needs no extra keyword — `l3isid <0-16777215>` exists
  only as an optional filter, and the unfiltered form lists every DvR
  interface with its `L2ISID`/`VLAN`/`GW IPv4` columns.

Commands that a given platform/release doesn't support (e.g. `show ist` on a
non-SMLT ERS, `show virtual-ist` on a non-vIST VOSS) are reported as warnings,
not failures.

## Design notes / limitations

* **Parsers are token-anchored, not column-anchored.** VOSS shifts table
  layouts between releases; the parsers key on stable tokens (port IDs,
  integer IDs, `up`/`down`, `c<vid>:` markers) and are covered by unit tests
  against fixture outputs. If one of your devices produces output the parser
  misreads, capture it with `--save-raw` and add it as a test fixture.
* **c-vid visibility**: for **DvR-enabled** L2VSNs the controllers see the
  whole domain via `show dvr interfaces`, so VLAN↔I-SID attachments are
  reliable there. For L2VSNs **without** a DvR interface (pure L2, no
  gateway IP) attachments are only visible on the devices that terminate
  them; the tool additionally reads the controllers' local `show i-sid` /
  `show vlan i-sid` plus fabric-wide I-SID existence via IS-IS. A service
  that terminates only on BEBs outside your `dvr_controllers` list shows up
  as `IN_FABRIC_NOT_ATTACHED` — that's the "verify by hand" bucket by
  design. You can add important BEBs to `dvr_controllers`; any VOSS node
  works as an additional state source.
* **ERS LACP-only trunks** (without MLT) are not listed as MLTs; their ports
  still appear in the Ports sheet with link state.
* **MLT member-up counts** are computed by cross-referencing member ports
  with the live port table rather than trusting the MLT status column —
  more reliable across firmware versions.
* Uplink detection needs LLDP enabled on the legacy switch (default on both
  platforms). Without LLDP data the audit still runs (with a per-switch
  warning); uplink columns stay 0.
* **ERS with menu-based console**: units configured with the menu instead of
  the CLI as default interface (`cmd-interface menu`) fail at login and are
  reported UNREACHABLE. Set `cmd-interface cli` on the unit (or audit it by
  hand) — the tool intentionally does not try to navigate the menu.
* A device whose SSH session dies mid-audit is abandoned after 2 consecutive
  transport failures instead of burning the full read-timeout on every
  remaining command; collected partial data is kept and the failures appear
  in Issues.

## Development

```bash
pip install -e '.[dev]'
pytest -v
```

The test suite runs entirely offline against captured CLI fixtures in
`tests/fixtures/` — including a full end-to-end pipeline test through
collectors, comparison and report writers.

### Project layout

```
switch_migrator/
├── cli.py              # argument parsing + orchestration
├── menu.py             # interactive toolkit menu (session: collect once, reuse)
├── config.py           # YAML config/inventory loading, credential resolution
├── connection.py       # netmiko SSH runner + offline replay runner
├── models.py           # dataclasses + comparison status model
├── compare.py          # VLAN↔I-SID comparison engine
├── config_extract.py   # VOSS running-config -> neutralized extract
├── config_generate.py  # ERS L2 model -> VOSS flex-UNI draft
├── isid.py             # per-VLAN I-SID resolution + decision worksheet
├── collectors/
│   ├── switch.py       # legacy switch collection (VOSS + ERS)
│   └── dvr.py          # DvR fabric-state collection + merge
├── parsers/
│   ├── voss_parsers.py # VOSS/Fabric Engine CLI parsers
│   ├── ers_parsers.py  # ERS/BOSS CLI parsers
│   └── common.py       # port-list expansion, LLDP block parser
└── report/
    ├── tables.py       # builds report tables once, shared by all renderers
    ├── migration.py    # port info + cabling sheets, migration commands
    └── excel.py        # xlsx + csv export; console.py renders to terminal
```
