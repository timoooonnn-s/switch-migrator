# Open questions

## Round 1 — answered 2026-11-19

| Q | Answer (user) | Consequence |
|---|---|---|
| Q1 migration shape | Mostly **ERS → Fabric Engine**, sometimes FE → FE. Usually a **move with consolidation**: switches are cut because fewer ports are used. | The plan must allocate ports across *fewer* new switches and say when they are full. ERS is the main source platform, so the ERS path needs real captures. |
| Q2 new-port rule | Manual today. **Real MLTs and servers on two switches start at port 1 upward; servers on one switch start at the last port downward** (24, 36, 48 – whatever the model has). | A deterministic two-ended allocator (D2). |
| Q3 ZTP baseline | ZTP sets ip domain, DNS, syslog, SSH, SNMP, probably mgmt. **This tool must produce: VLAN → I-SID conversion and the I-SID (or VLAN) config, port config for the new ports, and MLTs.** | Config scope is defined *by construction*: three blocks, nothing else (D3). |
| Q4 company standard | `port-preparer` has most of it. | Reuse its conventions and profiles, do not re-invent them (D4). |
| Q5 UNI model | **flex-UNI.** This tool is for **DC**. Office/branch is 90 %+ 1:1. | flex-UNI only; office/branch out of scope (D1). |
| Q6 cabling doc | **AixBOMS**, exports Excel / CSV / PDF. Column shapes described (see D5). | File importer, no scraping (D5). |
| Q7 hidden hosts | Could be all kinds: powered off, no LLDP and silent, passive, admin-down. | Reconciliation must handle every kind; doc-only links are first-class plan entries (D5). |
| Q19 version | Local main fast-forwarded to `origin/main`. | Version discipline still open (R12). |

Inferred by the council, **please confirm or correct**:

* **Q8 (is the fabric audit needed?)** — yes, but in a new role. ERS VLANs have
  no I-SID; on a DC move they already exist in the fabric, so the DvR read is
  *the source of the I-SID* for every ERS VLAN. The audit stays, as the I-SID
  resolver, not as a separate report the night depends on.

## Round 2 — answered 2026-11-24

| R | Answer (user) | Consequence |
|---|---|---|
| R1 reserved ports | The **last 4 or 6 ports** of a new switch are the uplinks (DAC/fibre). | Layout = ports + uplinks per run (`plan:` in config, `--new-ports/--new-uplinks`); the allocator never touches the last N. |
| R2 order in a block | The proposal (old switch, then old port) is fine. | Deterministic order; re-running gives the same plan. |
| R3 two links per side | Consecutive ports; **MLT id = 4xx of the first port**. | `standard.mlt_id_for_new_lag(first port)`. |
| R4 single-homed side | **Balance them** so both switches fill equally. | Alternating, deterministic (not random: the same input must give the same plan). |
| R5 teamed without LACP | **Two plain ports**, port config as it is. | Dual-homed block, same port number on both peers, no MLT. |
| R6 downstream switches | Unknown. | Treated like any other attachment; still open. |
| R7 unused / uncertain | Leave unused out, but **mark that there may be devices to check**; uncertain goes in, flagged. | Unused ports listed on the rack sheet as "check: cable plugged?"; uncertain rows flagged in the plan. |
| R8 pair full | Report it; usually known or decided from this output. | Several pairs on `--new-switch` are filled in order; the rest is reported as overflow. |
| R9 I-SID or VLAN config | The user's choice, **I-SID by default**. | Phase 3 renderer option, default flex-UNI I-SID. |
| R10 vIST / NNI | ZTP, or the user by hand. | Out of scope for this tool. |
| R11 MLT names | `f<port>` is fine, **make it easy to change**. | `plan.mlt_name` template in config.yaml, default `f{slot}{port}` (port 1/10 -> `f110`). |
| R12 port-preparer | **Copy what is needed**; port-preparer was an experiment. | D4 changed: `switch_migrator/standard.py` holds the copied conventions. |

## Round 2 — still open

### The port allocator (blocks phase 1)

**R1. Ports the allocator must never use on a new switch.** Which ports are
NNI / fabric uplinks, vIST, or ZTP `auto-sense` ports? Fixed per model (e.g.
"the 4 last SFP28/QSFP ports"), or different per switch? Which new models
are in use, with how many access ports each?

**R2. Order inside each block.** When several dual-homed servers start at
port 1: in which order? Proposal: old switch, then old port — stable, and
the techs can follow it. Alternatives: by host name, by VLAN, by rack.

**R3. A server with two links per switch** (4-link LAG): consecutive ports
(1–2 on both peers)? And is the MLT id then `4xx` of the **first** port
(port 1/1 → MLT 401)?

**R4. Single-homed servers: which of the two new switches?** Alternate to
balance them? Fill switch A first? Or does the old position decide (old
unit 1 → new A)?

**R5. "Ports on two switches" without an MLT.** A server with
switch-independent NIC teaming (active/backup, no LACP) on two ERS units:
does it become an SMLT, or two plain access ports at the **same port number**
on both peers?

**R6. Downstream switches and other infrastructure** on the old switches
(e.g. an ERS stack hanging off a DC leaf, `s129` style): same rule as servers,
or reserved / handled by hand?

**R7. Ports the tool thinks are unused** (`UNUSED` / `LIKELY UNUSED`) — leave
them out of the plan by default? And `UNCERTAIN` (down, but has passed traffic
or went down recently) — in the plan, flagged?

**R8. When the new pair is full** — the plan reports "needs another pair, N
ports over", or do you already know the target switches before planning?

### Config

**R9. "I-SID or VLAN config"** — when is it VLAN config on a flex-UNI DC leaf?
A VLAN that stays local (no I-SID)? The FE → FE case where the old box is
CVLAN? Something else?

**R10. vIST / SMLT pair and the NNI uplinks** of the new switches — set by ZTP,
by hand, or should this tool produce them?

**R11. MLT names** on the new switches. `port-preparer` saw `f<port>` for
server LAGs and `s<port>` for switch links, and still has open what they mean.
What should a new MLT be called?

**R12. Shared code with `port-preparer`.** Its `conventions.py` (I-SID,
MLT `4xx`, LACP key, VLAN names), `profiles/default.yaml` and `Binding` model
are exactly what this tool needs. Options:
* **a)** `switch-migrator` depends on `port-preparer` as a library (one
  `pip install` from git);
* **b)** extract a small shared package (`fabric-standard`) both depend on;
* **c)** copy the module and keep the copies in sync by hand.
Council recommendation: **b)** — but only once (a) has shown which pieces
are shared; start with (a).

### AixBOMS

**R13. One real export**, CSV *and* Excel if possible, of **one switch** (or
one rack). Blurred with same-length substitutes. Without it the importer is
guesswork.

**R14. The exact shape of `switch(unit)x(port)`.** For example
`gx-11-s72-p1(1)x(7)`? `gx-11-s72-p1 1x7`? Brackets literally? On a
standalone Fabric Engine switch, what does "unit" become?

**R15. Fibre (TX/RX).** One row per strand, so two rows per link?

**R16. Patch panels.** Does a row describe the whole link (switch port →
patch cable → … → patch cable → device port), or one row per hop through a
panel?

**R17. After the migration, who updates AixBOMS,** and what changes per
record — `von Kind ID` to the new switch/port, the patch cable numbers,
anything else? (This becomes the "documentation changes" sheet.)

### Still open from round 1

**R18 (was Q13). Where does it run?** RHEL version, Python version, is
`pip install` allowed? (`port-preparer` targets Python 3.11.)
*Partly answered (2026-11-25):* Linux x86_64, Python 3.11, pip can fetch from
the index (D13). Still open: RHEL version, and whether that index is PyPI or a
mirror. A mirror could serve `fabric-weft` and close D13's dependency-confusion
gap - one name rather than three since D14 merged migration-check in, and
switch-migrator itself is installed from the checkout either way.

**R19 (was Q15). The paper.** Printed or tablet, who fills it in, English or
German on the sheet?

**R20 (was Q9). How many real migrations has the tool carried**, and which
outputs were used on the night? Decides what gets frozen.

---

## Capture requests (still wanted)

Text output, not screenshots. Blurring is fine, but **replace names with
substitutes of the same length** (e.g. `gx-11-s72-p1` → `aa-00-a00-a0`) —
some tables are parsed by column position.

| # | Device | Commands | Why |
|---|---|---|---|
| C1 | New switch, fresh from ZTP | `show running-config`, `show sys-info`, `show interfaces gigabitEthernet state`, `show isis interface` | Baseline to subtract, which ports exist, which are NNI (R1) |
| C2 | New switch, finished, in production | `show running-config` – port, MLT and I-SID sections are enough | The standard by example, real LACP member-port syntax |
| C3 | Old ERS stack you will migrate | `show running-config`, `show vlan`, `show interfaces`, `show interfaces names`, `show mlt`, `show lldp neighbor`, `show mac-address-table`, `show sys-info` | No production ERS capture exists; ERS is the main source platform |
| C4 | Old FE/VOSS server leaf with LACP servers | `show running-config` (port/MLT/I-SID), `show interfaces gigabitEthernet name`, `show mlt`, `show interfaces gigabitEthernet fdb-entry` | Port names, FDB shape with SMLT |
| C5 | AixBOMS | export of the same switch as C3 or C4 (R13) | Importer design, and a matched pair to test reconciliation against |

C3 + C5 **of the same switch** would be the single most valuable thing you
can send: it is the first real end-to-end case.
