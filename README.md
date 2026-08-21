# switch-migrator

A migration toolkit for Extreme Networks switches: **VOSS / Fabric Engine
8.x–9.4.x** and **ERS / BOSS** stackables being moved onto an SPB fabric.

It reads the old switches and the fabric over SSH, tells you what would break,
produces the paperwork the migration night runs on, and checks the result
afterwards.

**Only `show` commands are ever sent.** The tool never changes a device
configuration, so it is safe against production at any time — including inside
the maintenance window. `--dry-run` prints the exact list it would send, for a
change board.

## What you run, and when

| Stage | Command | The question it answers |
|---|---|---|
| Weeks before | *(no extra flag)* | Does every VLAN on the old switches exist in the fabric? |
| | `--extract-config` | What does the new device's config need to look like? |
| Days before | `--migration-sheets` | What has to be re-patched, and what is on each port? |
| Window opens | `--health-check` | Is anything already broken that this would make worse? |
| During | `--generate-mlt` | The MLT config for the new switches, from the filled-in sheet |
| After | `--verify-migration` | Did every link come back up where it should? |
| Handover | `--handover` | One folder, with an index, for a reviewer or the change record |

Each has its own section below. Running `switch-migrator` with no arguments
opens an interactive menu that does all of it without flags.

## The audit

The audit is the tool's original job and still the core of it. Given a list of
switches it:

1. **reads the legacy switches** — which ports are up (with their LLDP
   neighbors), which of those are uplinks to your core, MLT/SMLT state including
   per-member link state, IST/vIST state, every VLAN, and on VOSS the local
   VLAN ↔ I-SID bindings;
2. **reads the fabric from the DvR controllers** — the domain-wide DvR
   interfaces with their L2 I-SID ↔ VLAN pairs (`show dvr interfaces`), which
   I-SIDs exist fabric-wide (`show isis spbm i-sid all`), and the controllers'
   own c-vid attachments (`show i-sid`, `show vlan i-sid`);
3. **compares** every VLAN on every switch against that fabric state and flags
   anything that would break during the migration.

### How a VLAN is matched to an I-SID

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
| `excluded_vlan_names` | Name globs to skip, e.g. `quarant*` for a local quarantine VLAN. |
| `unused_after_days` | How long a port must have been down to count as unused (default 30). |
| `mac_cap` | Learned MACs kept per port in the sheets (default 10). |
| `locations` | Maps switch names to sites, for `--split-by-location`. |
| `ssh` | Timeouts, retries, parallel workers, legacy-algorithm support. |
| `inventory` | Default inventory file, so a plain `switch-migrator` needs no `-i` (a `./switches.yaml` is picked up even without this key). |
| `commands` | Per-command spelling overrides (e.g. `'show mlt': 'show mlt all'`), applied at the runner so dry-run, raw capture and offline replay all see the same spelling. Replacements are validated read-only. |
| `console_server` | Terminal server for switches without a management IP: SSH to the console server, land on the switch's serial console, continue there (see below). |

### Console server (terminal server)

Switches that have no management IP (yet) can be reached through a terminal
server. The mechanics are template-driven, so both common flavors work:

```yaml
console_server:
  host: tsserver
  # Avocent-style - the console line rides in the SSH username
  username_template: "{username}:70{port}"
  # OR OpenGear-style - one SSH TCP port per line
  # tcp_port_template: "70{port}"
```

and in the inventory, per switch:

```yaml
switches:
  - name: new-leaf-01
    platform: voss
    console: "03"        # -> ssh admin:7003@tsserver
```

The tool SSHes to the terminal server, answers the switch's own console login
(including the ERS `Ctrl-Y` gate) with the switch credentials, and then runs
the normal collection over that console session. If the console server uses a
different account than the switches, set `SM_CONSOLE_USERNAME` /
`SM_CONSOLE_PASSWORD`.

### Profiles (`profiles.yaml`)

A profile bundles what one recurring scenario always needs - inventory, output
directory, collection settings, target switch - so "scenario A" is one load
instead of six prompts. Menu option `p` loads or saves them; on the command
line, `--profile NAME` applies one (explicit flags always win). Profiles never
hold credentials.

```yaml
profiles:
  site-a:
    inventory: inventories/site-a.yaml
    output_dir: output/site-a
    new_switch: leaf-a-01
    save_raw: true
    split_by_location: true
    location_groups:
      Frankfurt: [gx-11, gx-12]
```

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

For the full picture of the session layer — the Ctrl-Y login gate on ERS, the
`enable`/paging phases, retries and the circuit breaker, every place the two
platforms diverge, and a symptom-to-fix decision tree — see
**[docs/CONNECTIONS.md](docs/CONNECTIONS.md)**.

## Usage

Two interfaces over the same code: a menu for working through a migration, and
flags for scripting it.

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
  8  Snapshot              save this session's data, or load an earlier one
  9  Dry run               list every command a collection would send
  h  Health check          go/no-go before the window: what is already broken?
  m  Generate MLT blocks   new switches' MLT config from a filled-in cabling sheet
  v  Verify migration      after the window: check every re-patched link
  b  Handover bundle      one folder with every output and an index to hand over
  s  Settings              output directory, offline replay, manifest, target switch
  0  Quit
```

The menu holds a **session**: devices are collected **once** (option 2, which
asks whether to include fabric state, running-config and MAC tables), and every
output afterwards is produced from that same data — switching between use cases
costs no further SSH round-trips. Options that need data you didn't collect say
so instead of silently producing empty columns.

A few behaviors worth knowing:

* **`x` aborts anything.** At any prompt, `x` (or Ctrl-C) abandons the current
  action and falls back to the menu. A stray Enter at the menu re-shows it —
  quitting is an explicit `0`/`q`.
* **Tab completes paths.** Every file/directory prompt has readline tab
  completion.
* **Default inventory.** With no `-i`, the config's `inventory:` key (or a
  `./switches.yaml`) is picked up automatically.
* **Credentials are checked first.** Before the full collection, one quick SSH
  login against one device verifies the credentials — wrong ones fail in
  seconds and can be corrected on the spot, instead of costing a read-timeout
  per device (they are never blindly retried, so no lockout risk).
* **Every collection is auto-snapshotted** to
  `<output>/snapshots/snapshot-<stamp>.json` (toggle in Settings). A crash
  costs nothing: option 8 lists the snapshots and reloads one by number.

**All command-line flags below keep working unchanged** — the menu is an extra
entry point, not a replacement, so existing scripts and cron jobs are unaffected.

### Command line

```bash
# Standard audit: inventory file + Excel report + console summary
switch-migrator -c config.yaml -i switches.yaml

# A couple of switches without an inventory file (NAME:PLATFORM[:HOST])
switch-migrator -s old-access-01:ers -s old-agg-01:voss:10.1.1.20

# CSV export, raw CLI capture, and the detail tables on the console
switch-migrator -i switches.yaml --csv --save-raw -v

# Replay a --save-raw capture instead of touching any device
switch-migrator -i switches.yaml --offline output/raw

# Isolated environment: state report only, no fabric to compare against
switch-migrator -i isolated.yaml --no-fabric

# The new device's config, from the old one
switch-migrator -i switches.yaml --extract-config

# The migration paperwork, one cabling tab per site
switch-migrator -i switches.yaml --migration-sheets --new-switch new-sw-01 \
                --extract-config --split-by-location

# Collect once; rebuild any report from the snapshot later, no SSH
switch-migrator -i switches.yaml --migration-sheets --save-snapshot
switch-migrator --from-snapshot output/snapshot-20260812-140311.json \
                --migration-sheets --csv

# The migration night
switch-migrator -i switches.yaml --health-check          # before: go/no-go
switch-migrator --generate-mlt cabling.xlsx              # during: MLT config
switch-migrator --verify-migration cabling.xlsx          # after: every link

# For a change board: what would be sent (connects to nothing), and
# afterwards, what was sent and how it went
switch-migrator -i switches.yaml --dry-run
switch-migrator -i switches.yaml --manifest
```

## What the tool produces

Four kinds of output, each behind its own flag. All of them work with
`--offline` / `--save-raw` and from a snapshot, so nothing here needs a second
trip to the devices.

### Migration sheets (`--migration-sheets`)

Adds the two worksheets you take into the migration window, plus a commands
file. Every port gets a **sequential migration ID** (`P0001`, …) assigned across
the whole run — the key that ties both sheets together when several old
switches consolidate onto fewer new ones.

* **Port Info** (all ports) — Port ID, switch, port, device on the port (LLDP
  name / IP / SysDescr), MAC addresses, untagged VLAN, tagging, the
  **VLAN → I-SID pairs**, the flat VLAN/I-SID lists, admin & oper state, LACP,
  MLT ID & name, transceiver, media, uplink flag, usage and the VLAN source.
* **Cabling** (connected ports only — what actually gets re-patched) —
  deliberately wide, *one big paper*, so every row is self-contained at the
  rack, and laid out in the order the work happens:

  | | Columns |
  |---|---|
  | What the link is | Port ID, Usage, Type (access/mlt/uplink), Untagged VLAN, End device / neighbor |
  | Where it is now | **Rack (old)** ✎, Old switch, Old port |
  | Where it goes | **Rack (new)** ✎, **NEW switch** ✎, **NEW port** ✎, **NEW MLT ID** ✎, **NEW MLT name** ✎, **NEW VLAN** ✎ |
  | What to configure | Tagging, VLAN → I-SID, MLT ID, MLT name, MLT VLAN → I-SID, the flat VLAN/I-SID lists |
  | Evidence | MAC addresses, Media, Why, VLAN source |

  ✎ = filled in by hand. Those columns are shaded, unlocked and wide enough to
  write in; everything derived from the devices is locked so it cannot be
  edited by accident. **Rack (old)** and **Rack (new)** are new and deliberately
  empty — no switch knows which rack it is in.
* **`migration-commands-<stamp>.txt`** — per-port
  `show interfaces gigabitEthernet fdb-entry` commands to run on the **new**
  switch (each annotated with the Port ID, the
  old switch/port and the MACs to expect), a post-migration state overview, and
  — when combined with `--extract-config` — the neutralized device config ready
  to copy.

MAC addresses are capped at 10 per port with a `(+N more)` note. Pass
`--new-switch NAME` to name the target device in the commands file;
`--new-switch a,b` names several and turns the sheet's **NEW switch** column
into a dropdown of exactly those, so a target is picked rather than typed.

The workbook prints: landscape, fit to width, the header row repeated on every
page, and a page break whenever the old switch changes, so no rack's rows
straddle two sheets of paper.

#### VLAN → I-SID, and tagged vs untagged

Both sheets carry the port's VLANs and I-SIDs as **pairs**, not as two
independent lists:

```
735->2500735 (u), 100->2500100 (t), 174->(local), 2200->?
```

* `(u)` untagged, `(t)` tagged, `(t+u)` a c-vid that also takes the port's
  untagged traffic (VOSS flex-UNI).
* `(local)` — a VLAN with no fabric service by design; `(excluded)` — matched
  `excluded_vlans`; `?` — the tool looked and found nothing. **A VLAN is never
  dropped for want of an I-SID**: a question mark is a task, a blank cell is a
  trap.
* Untagged first, then by VLAN id. The flat `Port VLANs` / `Port I-SIDs`
  columns stay beside them for filtering and sorting.

Tagged-vs-untagged is what you actually configure on the new switch
(`c-vid <vlan> port …` versus `untagged-traffic port …`), so it matters more
than a bare PVID number — which a flex-UNI leaf does not really have. It can
only be read from the device's **running-config**, so `--migration-sheets`
pulls it as well (one extra command per device). Without it the tagging columns
stay empty rather than being guessed, and the **Coverage** sheet says so.

#### Is my data actually complete? (the Coverage sheet)

Several sources are optional — a release that rejects `show vlan members`, a
run without the running-config — and the sheet still comes out looking
finished, just with thinner VLAN columns. The **Coverage** sheet makes that a
number: per switch, how many ports have VLANs, I-SIDs and tagging at all, which
sources answered and which did not. Every row also carries a **VLAN source**
column, so an empty cell can be told apart from an unknown one.

Check it before the window. It is the sheet that would have caught a cabling
sheet reaching a data centre short of VLANs nobody knew were missing.

#### Is this port actually used?

Link state alone can't answer that — a momentarily-down MLT member looks exactly
like a dead port. Both sheets therefore carry a **Usage** column and the
**evidence** behind it, combining three signals that say something about *time*:

* **how long** the port has been in its current state (the `DATE` column of
  `show interfaces gigabitEthernet state`),
* whether it has **ever passed traffic** (interface counters, read together with
  the switch uptime — `0 packets` is only trusted on a long-running box),
* whether it belongs to an **MLT that is still forwarding**.

| Class | Meaning |
|---|---|
| `IN USE` | link up, or MACs learned |
| `IN USE - degraded` | down member of a still-forwarding MLT — a **fault on a live cable**, never filtered away |
| `UNCERTAIN` | down, but has passed traffic or went down recently (server reboot, link flap) |
| `LIKELY UNUSED` | down longer than the threshold, but the counters can't be trusted |
| `UNUSED` | down long, zero counters over a long uptime, or admin-disabled |

Nothing is ever silently dropped: likely-unused ports stay on the sheet and just
sort to the bottom, so the techs work top-down. The threshold is
`unused_after_days` in `config.yaml` (default 30).

VOSS has **no** `show mac-address-table`; the forwarding database is read with
`show interfaces gigabitEthernet fdb-entry`. The bare form (whole box, one
command) is tried first, and releases that insist on a port argument fall back
to querying only the ports that are operationally **up**. ERS/BOSS uses the
classic `show mac-address-table`.

### Splitting the cabling sheet by location (`--split-by-location`)

Switch names carry the site in their prefix, so the cabling sheet can be split
per location without anyone maintaining a second list of which box is where.
Each site's technicians then get a worksheet with only their own links on it.

Configure the sites once:

```yaml
locations:
  patterns:                       # first match wins
    "gx-11-*": "Frankfurt DC1"
    "gx-12-*": "Frankfurt DC2"
    "mu-*": "Munich"
  fallback_segments: 2            # unmatched: gx-11-s72-p1 -> "gx-11"
  groups:                         # which sites share one worksheet
    Frankfurt: ["Frankfurt DC1", "Frankfurt DC2"]
```

then `--split-by-location` turns the single **Cabling** sheet into one per
group:

```
Cabling sheet split by location:
  Frankfurt: gx-11-s72-p1, gx-11-s74-wu, gx-12-s01-p9
  Munich: mu-01-a
```

A location that is in **no** group gets its own worksheet, so *"separate
everything"* is the behaviour you get by writing no groups at all. To scope one
window differently without editing the config, `--location-group` replaces the
groups for that run (repeatable):

```bash
switch-migrator -i switches.yaml --migration-sheets \
    --location-group 'Frankfurt=Frankfurt DC*' --location-group 'South=Munich'
```

Two deliberate choices:

* **No combined sheet is written alongside the split ones.** This is a document
  people write into by hand — if a link appeared on both a per-site tab and an
  all-sites tab, two technicians could fill in two copies of the same row and
  one set of answers would be lost. Every link is on exactly one sheet.
* **The split keys on the OLD switch name**, because that is the only thing
  that exists when the sheet is written — the NEW columns are what gets filled
  in afterwards.

Only the cabling sheet is split. The port info sheet, the audit tables, the
health check and the verification stay whole.

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

## The migration itself

The audit tells you whether the configuration lines up. These three answer the
questions asked on the night.

### Before: health check (`--health-check`)

A different and more urgent question than the audit's: **is anything already
broken that the migration would make worse**, or that would hide behind the
migration and get blamed on it afterwards?

| Verdict | Meaning |
|---|---|
| `BLOCK` | do not start; fix this or re-plan the window |
| `WARN` | start, but know about it |
| `UNKNOWN` | the switch did not say enough to judge |
| `OK` | nothing found |

What blocks: an unreachable switch, no readable port state, a **degraded MLT**
(running on one leg — the first cable you unplug is an outage, not a re-patch),
a **down vIST** (the SMLT pair is not a pair right now), lost uplink
redundancy, and VLANs with nowhere to land in the fabric. What warns: an MLT
that is already fully down, ports the usage classification flagged as a fault
on a live cable, session problems that make the collected data less
trustworthy.

Every check is derived from the state the normal collection already gathers —
**no extra command is sent to any device** — so it costs nothing on top of the
audit you were running anyway. A `BLOCK` sets exit code 1, so a change pipeline
can gate on it.

### During: MLT blocks (`--generate-mlt`)

Reads the filled-in cabling sheet and emits the aggregations to create on the
new switches, in the two sections a VOSS box prints in its own
`show running-config`:

```
mlt 35 enable name "MLT035.srv"
mlt 35 member 1/11,1/12

interface mlt 35
smlt
lacp enable key 35
flex-uni enable
exit
```

The MLT id and name come from the sheet's **NEW MLT ID / NEW MLT name**
columns when the planner filled them in, and are carried over from the old MLT
when they didn't — so it produces something useful from a half-filled sheet and
gets more precise as the sheet does. `--no-smlt` emits plain single-switch MLTs
instead of SMLT pairs.

It refuses to guess where guessing is dangerous. **No I-SID, c-vid or VLAN
binding is ever emitted** — that is `--extract-config` and the I-SID decision
worksheet's job, and they have their own rules about what may be assumed.
Anything it only half understands is written into the file as a `# [!]` line
rather than silently configured: an aggregation of one member, an id claimed
twice on one switch, members facing three different neighbours, or an SMLT
that ended up on only one of the two peers (a single point of failure wearing
the costume of a redundant one). An uplink facing **two** neighbours is the
normal SMLT-pair shape and is not flagged.

### After: verification (`--verify-migration`)

Reads the same sheet back, collects the **new** switches — it takes their names
from the sheet, so no inventory is needed — and checks every link a technician
recorded:

| Result | Meaning |
|---|---|
| `PASS` | the port is up **and** at least one MAC the old port used to learn is now learned here (or the LLDP neighbour matches, when the old port had learned nothing). The same machine is talking on the new port. |
| `WARN` | the port is up, but nothing else lines up yet — no expected MAC back, or the neighbour, VLANs or MLT membership differ from the sheet |
| `FAIL` | the port is down, missing from the switch, or the switch could not be read |
| `PENDING` | no NEW switch/port in the sheet yet — not migrated, not a problem |

MACs age out in about five minutes and a machine that has not sent a frame
since the cutover has no entry anywhere, so *"no MAC yet"* is a **warn with
that explanation**, never a failure — re-run a few minutes later and most warns
turn into passes on their own. A `FAIL` sets exit code 1.

The summary sheet also lists **ports that are up on a new switch but appear in
no sheet row** — the other direction: a link somebody patched without writing
it down.

### Reading the sheet back

The cabling sheet has been through a data centre before it comes back, so
reading it is deliberately forgiving: columns are matched **by name, not
position** (add your own columns, reorder them, it does not matter), `.xlsx`
and `.csv` both work, port ids are normalised (`Port 1/7`, `1 / 7`, `1/7`),
rows nobody has filled in are *pending* rather than errors, and a cell it
cannot make sense of becomes a note on that row instead of killing the file.
The one thing it does complain about is a **half-filled row** — a NEW switch
with no NEW port, or the reverse — because that looks migrated and isn't.

In an `.xlsx`, **every** worksheet carrying an `Old port` column is read, so a
workbook that was split per location comes back whole; other sheets in the
workbook (Summary, Ports, …) are ignored. `--sheet-name 'Cabling Munich'`
restricts it to one site.

## Operations

Three things that have nothing to do with switches and everything to do with
running this on a real network: keeping the collected state, proving what the
tool sends, and recording what a run did.

### Snapshots (`--save-snapshot` / `--from-snapshot`)

Collecting is the expensive part: it needs credentials, network reach and a
moment when touching the switches is acceptable. Every report the toolkit
produces is a pure function of the collected state — so that state is worth
keeping.

`--save-snapshot` writes it all to one JSON file (default
`<output>/snapshot-<stamp>.json`). `--from-snapshot` rebuilds any report from
that file: no SSH, no credentials, no load on the devices. The reports are
byte-identical to the ones the live run produced.

That makes a snapshot three useful things at once:

* **the pre-migration record** — what the network looked like before you
  touched it, in a form you can diff or re-read months later;
* **a way to iterate** — try a different I-SID convention or a different
  `unused_after_days` and re-render, without another maintenance window;
* **something to hand over** — a colleague can produce the cabling sheet
  without reaching the switches at all.

A snapshot holds device data (hostnames, IPs, MAC addresses, LLDP neighbors,
and with `--extract-config` the running-config). Keep it wherever the raw
switch output belongs.

`--migration-sheets` reads the running-config too, for the tagged/untagged
columns — but **drops the text again** once that is extracted, so a snapshot
from a sheets run carries the derived VLAN bindings and not the RADIUS keys,
SNMP users and SPB identity the config itself contains. Only
`--extract-config`, which exists to produce a neutralized version of it, keeps
the original.

### Dry run (`--dry-run`)

Prints every command the run would send to each device — switches and DvR
controllers — and connects to nothing:

```
gx-01 (16 command(s))
    terminal more disable
    show interfaces gigabitEthernet state
    show mlt
    ...
```

The list is produced by running the **real collectors** against a runner that
answers every command with an empty string, so it cannot drift away from what
the tool actually does. Because empty answers send the collectors down every
fallback branch, what you see is the full set of commands that *could* be
sent — not just the subset one release happens to accept.

### Run manifest (`--manifest`)

Writes `<output>/manifest-<stamp>.json`: tool version, start/end time and
duration, every command sent to every device and whether it was accepted, the
errors and warnings raised, and the files produced. It never contains
credentials and never command output — only the command text and its outcome.
Turns "we checked before the migration" into something you can show afterwards.

### Handover bundle (`--handover`)

A migration leaves a trail across several files, and each of them assumes you
know what the others are. That is fine for whoever produced them and useless to
everybody else — the colleague reviewing the plan, the manager asking what this
window will do, or you, six months later, opening the folder for a site you
last touched in spring.

`--handover` writes `<output>/handover-<stamp>/`: every file the run produced,
**copied** (the originals stay where they were), plus an `index.html` that says
what each one is and shows the Summary, Coverage and Issues tables inline. The
page is plain self-contained HTML — no external assets — so it opens from a
file share, an email attachment or a USB stick, with no tool and no network.

In the menu it is option `b`, and it bundles whatever is already in the output
directory rather than re-running anything.

## Output files and exit codes

Output goes to `./output/` by default:

* `migration-audit-<timestamp>.xlsx` — one workbook, color-coded, filterable,
  with a frozen header row. Always: **Summary**, **VLAN vs Fabric**, **Ports**,
  **MLTs**, **Fabric I-SIDs**, **Coverage**, **Issues**. Plus **Port Info** and **Cabling**
  with `--migration-sheets` (one `Cabling <site>` tab per location with
  `--split-by-location`), **Health Summary** / **Health Check** with
  `--health-check`, **Verification Summary** / **Verification** with
  `--verify-migration`,
* `migration-commands-<timestamp>.txt` with `--migration-sheets`,
* `config/<device>.cfg` (+ `<device>.isid-decisions.txt` for ERS) with
  `--extract-config`; `config/mlt-blocks.cfg` with `--generate-mlt`,
* `csv-<timestamp>/*.csv` with `--csv` — one file per sheet,
* `snapshot-<timestamp>.json` with `--save-snapshot`,
* `manifest-<timestamp>.json` with `--manifest`,
* `handover-<timestamp>/` with `--handover` — every file above plus an
  `index.html` tying them together,
* `raw/<device>/<command>.txt` with `--save-raw`,
* `switch-migrator.log`.

`--no-excel` skips the workbook (useful with `--csv`); `-v` also prints the detail tables to the console.

**Exit codes** (scriptable): `0` = clean, `1` = errors found (unreachable
device, DvR read failure, any red comparison result, a health-check `BLOCK`, or
a failed link in the verification), `2` = config error.

### Reading the report

* **Summary** — one line per switch: ports up/total, **uplinks up/total**,
  MLT count and MLTs with down members, IST/vIST session state, VLAN
  comparison counters (ok/warn/error).
* **VLAN vs Fabric** — the core sheet: per VLAN the expected convention
  I-SID(s), what the DvR controllers actually attach, the matched I-SID and the status
  from the table above. This is your migration checklist.
* **Coverage** — how complete the collected data is per switch: ports with
  VLANs / I-SIDs / tagging, and which sources answered. Read this before
  trusting an empty cell anywhere else.
* **Issues** — flat, filterable list of everything that needs a human:
  unreachable devices, down IST sessions, MLTs with down members, VLAN
  mismatches.

## Commands sent to the devices

Every device first gets `enable` (both platforms log in at user-EXEC `>`, where
whole command trees the tool needs simply do not exist) and its paging-disable
command. Then, per platform:

| Read | VOSS | ERS / BOSS |
|---|---|---|
| paging off | `terminal more disable` (fallback `term more dis`) | `terminal length 0` |
| port state | `show interfaces gigabitEthernet state` **and** `... interface` | `show interfaces` |
| aggregation | `show mlt` | `show mlt` |
| peer link | `show virtual-ist` | `show ist` |
| VLANs | `show vlan i-sid`, `show vlan basic`, `show vlan members` | `show vlan` (incl. `Port Members`) |
| per-port VLAN/I-SID | `show interfaces gigabitEthernet i-sid` | — |
| neighbors | `show lldp neighbor summary` → `show lldp neighbor` | `show lldp neighbor` → `... summary` |
| learned MACs † | `show interfaces gigabitEthernet fdb-entry` | `show mac-address-table` |
| optics † | `show pluggable-optical-modules basic` | — |
| traffic counters † | `show interfaces gigabitEthernet statistics` | — |
| uptime † | `show sys-info` | `show sys-info` |
| config ‡ | `show running-config` | `show running-config` |

† only with `--migration-sheets` or `--verify-migration` (the MAC, optic and
usage columns need them)  ‡ with `--extract-config` **or**
`--migration-sheets` — the config is the only source of tagged-vs-untagged

On the **DvR controllers**: `show dvr interfaces`, `show isis spbm i-sid all`,
`show i-sid`, `show vlan i-sid`.

A plain audit run is **11 commands** per VOSS switch and **7** per ERS; with
`--migration-sheets` and `--extract-config` it is **16** and **10**. Each DvR
controller gets **5**. Two commands separated by `→` are a fallback chain — the
second is only sent if the device rejects the first.

`show vlan members` (VOSS) and the `Port Members` line of `show vlan` (ERS)
feed the per-VLAN member-port column of the inventory report. Both are optional
and silently skipped on releases that don't support them.

`--dry-run` prints the exact list for your inventory, produced by the real
collectors rather than copied from this table.

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
  table that includes the down `REASON` column (shown in the Ports sheet) and
  the `DATE` of the last state change, which is what the used/unused
  classification rests on. It has no `DESCRIPTION` column, so the
  `... interface` variant is read as well and its media type merged in; on a
  release that has only one of the two, that one is used and the other's
  columns stay empty rather than the run failing.
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
* **Wrapped port lists.** Both platforms break a long member list after a
  comma onto a continuation line. Port lists are validated per element and the
  member parsers absorb continuation lines, so a VLAN whose members wrapped
  keeps all of them. (Before this, the trailing comma made the whole list
  fail to parse and the VLAN lost *every* port, not just the wrapped ones.)
* **Tagged vs untagged comes only from the running-config.** No `show` command
  states it reliably on either platform. Without the config the tagging columns
  stay empty rather than being guessed, and Coverage reports it.
* **A VLAN in the running-config that `show vlan` never listed** is adopted
  into the switch's VLAN set, so it reaches the fabric comparison instead of
  ending up as an unresolved `?` on the sheet.
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

`docs/CONNECTIONS.md` documents the connection layer in depth (both platforms,
its invariants, and how to add a third one).

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
├── usage.py            # is this port actually in use? (evidence-based)
├── health.py           # pre-migration go/no-go, derived from collected state
├── location.py         # switch name -> site, and which sites share a sheet
├── cabling_sheet.py    # read the filled-in cabling sheet back (.xlsx/.csv)
├── mlt_generate.py     # cabling sheet -> new switches' MLT config blocks
├── verify.py           # post-migration: did every link come back up?
├── snapshot.py         # save/load the whole collected state as JSON
├── manifest.py         # per-run audit trail: commands, outcomes, files
├── handover.py         # one folder + index.html to hand the run over
├── collectors/
│   ├── switch.py       # legacy switch collection (VOSS + ERS)
│   └── dvr.py          # DvR fabric-state collection + merge
├── parsers/
│   ├── voss_parsers.py # VOSS/Fabric Engine CLI parsers
│   ├── voss_config.py  # VOSS running-config -> per-port VLAN/I-SID + tagging
│   ├── ers_parsers.py  # ERS/BOSS CLI parsers
│   ├── ers_config.py   # ERS running-config -> L2 model + tagging
│   └── common.py       # port-list expansion, LLDP block parser
└── report/
    ├── tables.py       # builds report tables once, shared by all renderers
    ├── migration.py    # port info + cabling sheets, migration commands
    ├── migration_tables.py  # health-check and verification tables
    ├── progress.py     # live per-device progress during collection
    └── excel.py        # xlsx + csv export; console.py renders to terminal
```
