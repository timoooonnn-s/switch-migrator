# Connections — how switch-migrator talks to VOSS and to ERS

**Audience:** a human maintaining this tool, and an LLM asked to change,
debug or extend it. Everything here is derived from the code in this
repository; every claim names the file and symbol it comes from, so it can be
checked and so it stays checkable when the code moves.

**Scope:** the *session layer* — everything between "we have a hostname and a
credential" and "we have the raw text of a `show` command". That is where the
two platforms genuinely differ. What happens to the text afterwards (parsing,
comparison, reporting) is covered only where the platform split reaches into
it.

Two words are used precisely throughout:

* **platform** — `Platform.VOSS` (VOSS / Fabric Engine, ex-Avaya VSP) or
  `Platform.ERS` (ERS / BOSS stackables). The tool's own term, from
  `models.py`.
* **vendor** — colloquially the same thing here. Both families are Extreme
  today; they are two different operating systems with two different SSH
  stacks, two different login rituals and two different CLIs. Treat them as
  two vendors, because operationally they behave like it.

---

## 1. File map

| File | What it owns |
|---|---|
| `switch_migrator/connection.py` | **The whole connection layer.** Runner interface, the live SSH runner, the offline and dry-run runners, legacy-algorithm enablement, the ERS login handler, error detection, retry and circuit breaker. |
| `switch_migrator/config.py` | `SshSettings` (every tunable), `Credentials`, `Platform` parsing from YAML/CLI (`_make_target`), credential resolution (`get_credentials`). |
| `switch_migrator/models.py` | `Platform` enum; `SwitchAudit.reachable/errors/warnings` — where connection outcomes land. |
| `switch_migrator/cli.py` | `make_runner()` (which runner for this run), `audit_one_switch()` (per-device isolation), `run_collection()` (parallelism), `collect_fabric()` (DvR sessions). |
| `switch_migrator/collectors/switch.py` | Platform dispatch for *which commands* get sent, plus fallback chains. |
| `switch_migrator/collectors/dvr.py` | DvR controller command set (always VOSS). |
| `switch_migrator/parsers/{voss,ers}_parsers.py`, `parsers/common.py` | Per-platform and shared output parsing. |
| `switch_migrator/report/progress.py`, `manifest.py`, `health.py` | Where connection facts surface to the user. |
| `tests/test_connection.py` | The behavioural contract of this layer, as executable tests. |

---

## 2. The runner contract

Everything that touches a device goes through one tiny interface,
`BaseRunner` (`connection.py:147`):

```python
class BaseRunner:
    name: str                     # device name, used in logs and raw paths
    setup_warnings: list[str]     # session-setup problems, merged into the audit
    command_log: list[CommandRecord]   # every command sent, in order
    on_command: Callable[[str, str, int], None] | None   # progress hook

    def run(self, command: str) -> str: ...   # raw output, or raise CommandError
    def close(self) -> None: ...
```

Three consequences worth internalising:

1. **The runner is the single choke point for all device traffic.** That is
   why the progress display, the raw capture, the run manifest and the
   read-only proof (`--dry-run`) all hang off it instead of being threaded
   through every collector. If you add a code path that talks to a device
   without going through a runner, you have silently broken all four.
2. **Collectors are platform-aware, runners are platform-agnostic — almost.**
   `SshRunner` and `DryRunRunner` take a `Platform` (they need it for the
   netmiko driver and the paging command); `OfflineRunner` does not. Nothing
   above the runner ever asks a runner what platform it is.
3. **Two exception types, two very different meanings.** Getting this wrong is
   the most common way to make the tool lie about a device:

| Exception | Raised when | Meaning | Retried? |
|---|---|---|---|
| `ConnectionFailed` (`connection.py:45`) | during construction of a runner — SSH, auth, prompt | the device was never usable | connect-level retries only (`ssh.retries`), **never** for auth |
| `CommandError` (`connection.py:108`) | in `run()` — device rejected the command, or transport died | *this command* failed; the session may still be fine | transport failures only (`ssh.command_retries`); a device **rejection** is never retried |

A rejected command is a *fact about the release*, not a flake. Re-sending
`show virtual-ist` to a box that answered `% Invalid input` cannot produce a
different answer, so `run()` deliberately does not.

---

## 3. The three runners, and which one runs

`make_runner()` (`cli.py:207`) picks in this order:

```python
if args.dry_run:  return DryRunRunner(name, platform)   # connects to nothing
if args.offline:  return OfflineRunner(name, args.offline)   # replays files
return SshRunner(name, host, platform, creds, cfg.ssh, raw_dir)  # live
```

| Runner | Source of output | Used by | Platform-aware? |
|---|---|---|---|
| `SshRunner` (`connection.py:269`) | a live netmiko session | normal runs | yes — driver, paging command, ERS login class |
| `OfflineRunner` (`connection.py:521`) | `<raw_root>/<device>/<command_slug>.txt` | `--offline`, and **every test in the suite** | no |
| `DryRunRunner` (`connection.py:546`) | returns `""` for everything | `--dry-run` | yes — seeds the paging command |

`command_slug()` (`connection.py:115`) is the shared key: it lowercases a
command and replaces every non-alphanumeric run with `_`, so
`show interfaces gigabitEthernet state` ⇄
`show_interfaces_gigabitethernet_state.txt`. `--save-raw` writes with it and
`--offline` reads with it, which is why a capture can be replayed without any
translation step.

`DryRunRunner` deserves a note because it is the tool's read-only proof.
Returning `""` for every command walks the collectors down **every** fallback
branch, so the printed list is the full set of commands the tool *could* send
to that device — not the subset one particular release happens to accept. And
because it is produced by running the real collectors, it cannot drift away
from what the tool actually does.

---

## 4. Every place the vendor changes behaviour

This is the complete list. If you are adding a platform, this table *is* your
work plan. If you are debugging one vendor, the bug is almost certainly at one
of these branch points.

| # | Location | Branch | Why |
|---|---|---|---|
| 1 | `connection.py:28` `NETMIKO_DEVICE_TYPE` | `VOSS → "extreme_vsp"`, `ERS → "extreme_ers"` | netmiko driver: prompt patterns, paging, `enable` behaviour |
| 2 | `connection.py:183` `_PAGING_DISABLE` | VOSS `terminal more disable` → fallback `term more dis`; ERS `terminal length 0` | different CLI spellings; VOSS has no `terminal width` |
| 3 | `connection.py:408` in `_connect()` | `if platform is Platform.ERS: connect_cls = _patient_ers_class()` | the Ctrl-Y login gate; the VOSS path is untouched |
| 4 | `collectors/switch.py:38` | `if VOSS: _collect_voss() else: _collect_ers()` | entirely different command sets |
| 5 | `collectors/switch.py:219` `_collect_fdb()` | `if not VOSS:` → `show mac-address-table`; VOSS → `show interfaces gigabitEthernet fdb-entry` (bare, then per-up-port) | VOSS has no `show mac-address-table` |
| 6 | `collectors/switch.py:336` in `_enrich_migration_fields()` | VOSS-only: `show pluggable-optical-modules basic`, `show interfaces gigabitEthernet statistics` | no ERS equivalent used |
| 7 | `collectors/switch.py:385` in `_enrich()` | LLDP command **order**: VOSS tries `summary` first, ERS tries the block form first | each platform's native form first; the other is a fallback |
| 8 | `parsers/` | `voss_parsers` vs `ers_parsers`, shared helpers in `common.py` | different table layouts |
| 9 | `compare.py:74`, `compare.py:129` | VOSS-only local VLAN↔I-SID binding logic; `LOCAL_ONLY` verdict is VOSS-only | ERS has no I-SID concept, so absence from the fabric *is* the finding |
| 10 | `report/migration.py:205`, `cli.py:386` | VOSS config is filtered/neutralized; ERS config is translated to a VOSS flex-UNI draft | migration direction is ERS/VOSS → VOSS |
| 11 | `cli.py:306,347`, `menu.py:539` | DvR controllers and post-migration verification targets are **hardcoded** `Platform.VOSS` | DvR controllers are always Fabric Engine; new switches are assumed VOSS (override with `-s`) |

**Two `else`-shaped traps.** Branch 4 and branch 5 are written as "VOSS or
everything else". A third platform added to the enum would therefore be
*silently collected as ERS* and *silently given `show mac-address-table`*,
with no error anywhere. Branches 1 and 2 are dict lookups and would raise
`KeyError` instead — loudly, which is better. If you add a platform, convert
4 and 5 to explicit dispatch rather than relying on the `else`.

---

## 5. Life of a session, step by step

Both vendors follow the same five phases. The per-phase notes say where they
diverge.

### Phase 0 — before any socket: legacy algorithms

`SshRunner.__init__` calls `enable_legacy_ssh_algorithms()`
(`connection.py:65`) when `ssh.legacy_algorithms` is true (the default). It
**appends** to paramiko's `Transport._preferred_kex` / `_preferred_ciphers` /
`_preferred_keys`:

* kex: `diffie-hellman-group14-sha1`, `...group-exchange-sha1`, `...group1-sha1`
* ciphers: `aes256-cbc`, `aes192-cbc`, `aes128-cbc`, `3des-cbc`
* host keys: `ssh-rsa`, `ssh-dss`

Three properties that are load-bearing and are each covered by a test:

* **Appended, never prepended.** Modern algorithms keep priority, so VOSS
  switches and DvR controllers negotiate exactly what they would have anyway;
  only a server that offers nothing better falls back to the legacy set.
  (`test_legacy_algorithms_appended_not_prepended`)
* **Idempotent and thread-safe.** A module-level flag under `_legacy_lock`;
  it mutates a *process-wide* paramiko class, so it must only ever happen
  once even though runners are constructed from a thread pool.
  (`test_enable_legacy_is_idempotent`)
* **Only what this paramiko build implements** is added, and anything missing
  is logged as a warning pointing at the `paramiko>=3.4,<4` pin.

Why the pin: **paramiko 4.x removed `ssh-dss`**, which the oldest ERS/BOSS
boxes still present as their host key. `test_paramiko_pin_still_excludes_the_release_that_dropped_ssh_dss`
asserts the requirement text itself, in both `requirements.txt` and
`pyproject.toml`, so an innocent dependency bump cannot quietly strand the old
gear.

Nothing here touches the OS, the device, `/etc/ssh`, or system-wide crypto
policy. It is entirely inside this process's paramiko.

### Phase 1 — connect (`SshRunner._connect`, `connection.py:376`)

Parameters worth knowing (all from `SshSettings`):

| Param | Value | Why |
|---|---|---|
| `device_type` | `NETMIKO_DEVICE_TYPE[platform]` | branch 1 |
| `conn_timeout` | `ssh.conn_timeout` (20 s) | TCP/SSH establishment |
| `banner_timeout`, `auth_timeout` | `max(15, conn_timeout)` | slow boxes print long MOTDs |
| `read_timeout_override` | `ssh.read_timeout` (60 s) | per-command read ceiling |
| `fast_cli` | **always `False`** | old ERS gear chokes on netmiko's fast path |
| `session_log` | in-memory `BytesIO` | so a failure can show what the device actually said |
| `session_log_record_writes` | `False` | never record what we send (credentials) |
| `global_delay_factor` | only if ≠ 1.0 | slow ERS CPUs |
| `default_enter` | only if set | ERS/BOSS boxes that need `\r\n` |
| `disabled_algorithms` | only if set | last-resort pin-*out* of a badly implemented modern algorithm |

**ERS divergence:** if the platform is ERS, `_patient_ers_class()` is built and
instantiated **directly** rather than via `ConnectHandler` — netmiko only
dispatches by string name, so a subclass cannot be selected any other way. If
that import fails, `connect_cls` is `None` and the stock class is used, so a
netmiko layout change degrades instead of crashing.

Retry policy:

* `NetmikoAuthenticationException` → **raise immediately, never retry.** This
  is deliberate: retrying bad credentials across an inventory locks out the
  account on every box at once.
* `NetmikoTimeoutException` / `OSError` → retry up to `ssh.retries` with a
  2 s, 4 s, … backoff.
* anything else (e.g. paramiko's `Incompatible ssh peer (no acceptable kex)`,
  or netmiko's `ReadTimeout` for a prompt that never appeared) → raise
  immediately. Retrying an algorithm mismatch is pure waiting.

After a successful connect the in-memory session log is detached
(`self._conn.session_log.session_log = None`) so it stops growing with every
command's output — DvR controllers can emit very large tables.

### Phase 1b — the ERS login gate (`_patient_ers_class`, `connection.py:193`)

This is the single most important piece of ERS-specific code, so it is worth
reading closely.

ERS/BOSS gate the CLI behind **`Enter Ctrl-Y to begin`**, and many boxes stay
completely **silent** after SSH authentication until they receive a keystroke.
netmiko's stock `special_login_handler` *reads before sending anything*, so
against a silent box it times out and the session never really enters the OS.
The symptom in the field is `ReadTimeout: Pattern not detected: '(?:\#|>)'`,
which reads like a cipher problem and is not one.

`PatientExtremeErs` fixes both halves:

* **`special_login_handler`** writes a RETURN *first* to wake the box, then
  loops up to six times reading for any of: `sername`, `ssword`,
  `Ctrl-Y`/`Ctrl Y`, `Press ENTER`, `Menu`, or a real prompt. Each is answered
  appropriately (Ctrl-Y → `\x19` then RETURN; Menu → `\x03`; username/password
  → the credential). A read that *throws* is treated as "still silent" and
  answered with another Ctrl-Y nudge. It never hard-fails: the next phase
  retries and produces the friendlier error.
* **`session_preparation`** retries `set_base_prompt()` three times, clearing
  the buffer and re-nudging Ctrl-Y between attempts, before re-raising the last
  exception. Only then does it set terminal width and disable paging.

Covered by `test_patient_ers_special_login_presses_ctrl_y`,
`test_patient_ers_class_retries_then_succeeds` and
`..._gives_up_and_reraises`. Those tests skip when netmiko is not installed
(the class is a netmiko subclass), with a reason that says so — a skip there
means a broken environment, not a broken tool.

**Known non-goal:** a unit configured with `cmd-interface menu` as its default
interface. The handler sends Ctrl-C at a `Menu` prompt, but the tool
deliberately does not try to navigate a menu UI; such a box is reported
UNREACHABLE. Fix it on the device (`cmd-interface cli`) or audit it by hand.

### Phase 2 — enter privileged EXEC (`_ensure_privileged`, `connection.py:298`)

**Both platforms** log in at user EXEC (`>`). On VOSS 8.x,
`show interfaces gigabitEthernet ...` and `show lldp ...` are privileged-only,
while `show mlt` / `show vlan` / `show virtual-ist` work in both modes. That
mix is exactly what made the failure confusing: some commands worked, so it
looked like release variance rather than a privilege problem. netmiko's VSP and
ERS drivers do not send `enable` on their own, and an operator types `ena`
interactively without thinking about it.

So: read the prompt; if it ends in `#`, done. Otherwise send `enable` and read
again. On VOSS/BOSS this is a plain mode switch — there is no separate enable
password for the logged-in account. If the prompt still is not `#`, a
`setup_warnings` entry is added naming the failure class and telling the
operator to check the account's access level. It is a warning, not a fatal
error, because the commands that *do* work in user EXEC still produce useful
data.

### Phase 3 — disable paging, and verify it (`_ensure_paging_disabled`, `connection.py:344`)

netmiko sends a paging-disable command too, but (a) it only verifies the
command **echo**, not whether the device accepted it, and (b) it runs *before*
the session is privileged. If the pager stays active, every long output stalls
at `--More--` **and the stuck pager swallows the next command's characters** —
one missed paging command corrupts the rest of the session, not just one
output.

So the tool sends it again explicitly, now privileged, and checks the device's
actual answer with `looks_like_error()`. VOSS gets a second spelling
(`term more dis`, field-verified) if the first is rejected. A rejection of all
spellings becomes a `setup_warnings` entry saying long outputs may stall.

No other session-tuning command is sent. VOSS has no `terminal width`, and an
unknown command desyncs the channel.

### Phase 4 — run commands (`SshRunner.run`, `connection.py:469`)

```
if self._dead: raise CommandError("session abandoned")     # circuit breaker
_log_start()                                                # fires on_command
for attempt in range(ssh.command_retries + 1):
    try: output = conn.send_command(command, read_timeout=ssh.read_timeout)
    except Exception:                                       # transport
        _recover_channel()                                  # send "q\n", drain
        if attempts left: sleep(1s, 2s, ...); continue
        _transport_failures += 1
        if _transport_failures >= ssh.max_transport_failures: self._dead = True
        raise CommandError("(transport ...)")
    break
_transport_failures = 0                                     # success resets it
if raw_dir: write <slug>.txt                                # --save-raw
if looks_like_error(output): raise CommandError(command, output)   # rejection
return output
```

Four behaviours to keep straight:

* **`_recover_channel()`** (`connection.py:459`) sends `q\n` and drains the
  buffer. A `ReadTimeout` is most often a stuck `--More--`; quitting it stops
  the *next* command from being eaten. (`test_stuck_pager_is_quit_after_transport_failure`)
* **The circuit breaker.** `max_transport_failures` (default 2) *consecutive*
  transport failures mark the session `_dead`; every remaining command then
  fails instantly instead of burning a full `read_timeout` each. A box that
  dies mid-audit costs seconds, not minutes, and the partial data already
  collected is kept. (`test_circuit_breaker_abandons_dead_session`)
* **A success resets the counter** — the breaker is for a session that has
  stopped answering, not for one flaky read.
  (`test_circuit_breaker_resets_on_success`)
* **Raw capture happens before error detection**, so a rejection is captured
  too and shows up in an offline replay exactly as the device answered.

### `looks_like_error()` — how a rejection is recognised (`connection.py:123`)

Two passes, because VOSS makes the naive version wrong:

1. the first 200 characters, lowercased, are searched for `_ERROR_MARKERS`
   (`% invalid input`, `% incomplete command`, `% unrecognized command`,
   `% cannot modify`, `error: invalid`, `invalid command`,
   `ambiguous command`);
2. **then every line** is checked for a `%`-prefixed line containing one of
   `invalid`, `incomplete`, `unrecognized`, `ambiguous`, `not allowed`,
   `cannot modify`.

Pass 2 exists because VOSS prints a `Command Execution Time:` banner framed by
84-character rules — over 200 characters — *before* the error, so a head-only
scan misses it entirely. The `%`-prefix requirement is what keeps ordinary
table data containing a percent sign (`utilization: 42 %`) from being read as
an error. Both cases are pinned by tests
(`test_looks_like_error_behind_banner`, `test_looks_like_error_negative`).

This function is shared by `SshRunner` and `OfflineRunner`, which is why a
captured rejection replays as a rejection.

### Phase 5 — close

`close()` calls `disconnect()` inside a bare `except` — teardown is
best-effort and must never mask the real result. `audit_one_switch()` closes in
a `finally`, and in an outer `finally` copies `runner.command_log` into the
manifest sink and reports to the progress display. So the command log survives
even a crashed collection.

---

## 6. VOSS profile (VOSS / Fabric Engine, and every DvR controller)

| Aspect | Value |
|---|---|
| netmiko driver | `extreme_vsp` |
| Login | ordinary SSH; modern algorithms |
| Prompt | `hostname:1>` → `enable` → `hostname:1#` |
| Paging off | `terminal more disable`, fallback `term more dis` |
| Banner quirk | `Command Execution Time:` block before output, >200 chars |
| Special handling | none at the transport layer |

Command set (`_collect_voss`, plus `_enrich*`):

| Purpose | Command | Notes |
|---|---|---|
| port state | `show interfaces gigabitEthernet state` | has `REASON` and `DATE`; **no** `DESCRIPTION` |
| port media | `show interfaces gigabitEthernet interface` | merged in for the media column |
| aggregation | `show mlt` | one call yields four tables: Mlt Info, LACP, data-path, ENCAP |
| peer link | `show virtual-ist` | absent on non-vIST boxes → warning, not failure |
| VLANs | `show vlan i-sid`, `show vlan basic`, `show vlan members` | last one optional/quiet |
| per-port I-SID | `show interfaces gigabitEthernet i-sid` | catches flex-UNI/switched-UNI services |
| neighbors | `show lldp neighbor summary` → `show lldp neighbor` | summary first: one line per neighbor |
| MACs † | `show interfaces gigabitEthernet fdb-entry` → per-up-port | VOSS has no `show mac-address-table` |
| optics † | `show pluggable-optical-modules basic` | |
| counters † | `show interfaces gigabitEthernet statistics` | |
| uptime † | `show sys-info` | |
| config ‡ | `show running-config` | |

† `--migration-sheets` / `--verify-migration` only  ‡ `--extract-config` only

Two connection-relevant design decisions hide in that list:

* **The plain `show interfaces gigabitEthernet` is deliberately not used.** It
  prints several full-width sections per port — 2000+ lines on a large stack —
  which is slow and can desync the session. Both variants that *are* used are
  one narrow row per port.
* **The per-port FDB fallback is bounded by up-ports.** If a release rejects
  the bare `fdb-entry` form, the tool asks only operationally-up ports (a down
  port has learned nothing), keeping a 48-port box from costing 48 commands.

DvR controllers (`collectors/dvr.py`) are always VOSS and get four commands:
`show dvr interfaces`, `show isis spbm i-sid all`, `show i-sid`,
`show vlan i-sid`. `collect_fabric()` gives them extra read headroom
(`read_timeout` raised to at least 120 s) because the fabric-wide I-SID listing
is large. A controller counts as an *authoritative* source only if
`show isis spbm i-sid all` succeeded; otherwise its data is merged but flagged
partial, so a half-read controller can never manufacture a false
`MISSING_ON_DVR`.

---

## 7. ERS / BOSS profile

| Aspect | Value |
|---|---|
| netmiko driver | `extreme_ers`, subclassed as `PatientExtremeErs` |
| Login | `Enter Ctrl-Y to begin`, often preceded by silence |
| SSH algorithms | frequently SHA-1 kex, CBC ciphers, `ssh-rsa`/`ssh-dss` host keys |
| Prompt | `hostname>` → `enable` → `hostname#` |
| Paging off | `terminal length 0` (no fallback spelling) |
| `fast_cli` | must stay `False` |
| Known dead end | `cmd-interface menu` units — reported UNREACHABLE by design |

Command set (`_collect_ers`, plus `_enrich*`):

| Purpose | Command | Notes |
|---|---|---|
| port state | `show interfaces` | ADMIN = `Enable`/`Disable`, OPER = first `Up`/`Down` after it |
| aggregation | `show mlt` | 59xx adds a KEY column; unconfigured slots are dropped, not reported as dead MLTs |
| peer link | `show ist` | absent on access boxes → expected, silenced with `absent_ok` |
| VLANs | `show vlan` | includes indented `Port Members:` continuation lines |
| neighbors | `show lldp neighbor` → `show lldp neighbor summary` | block form first; ERS rejects `summary` |
| MACs † | `show mac-address-table` | |
| uptime † | `show sys-info` | |
| config ‡ | `show running-config` | translated to a VOSS flex-UNI draft |

ERS has no I-SID concept, so there is no per-port I-SID command and no
`LOCAL_ONLY` comparison verdict — for an ERS VLAN, absence from the fabric *is*
the actionable finding.

### The four ERS escape hatches, in the order to try them

All four live in `ssh:` in `config.yaml`. Start at the top; each one costs
something.

1. **`legacy_algorithms: true`** (default). Fixes
   `Incompatible ssh peer (no acceptable kex/host key algorithm)`. Costs
   nothing — modern devices keep negotiating what they always did.
2. **`global_delay_factor: 2`** (or 3). Fixes a slow CPU that misses prompts.
   Costs time on **every** device in the run, so raise it only after the
   default has actually failed.
3. **`default_enter: "\r\n"`**. Fixes boxes that ignore a bare `\n` and
   therefore never answer prompt detection at all.
4. **`disabled_algorithms: {kex: [...], ciphers: [...], keys: [...]}`**. Last
   resort. Use only when the log shows the channel connected but returned
   garbage — that is a modern algorithm the device negotiates and implements
   badly. Pin it *out*.

---

## 8. Errors: taxonomy, message shape, and where each one surfaces

### The failure message is deliberately built, not just re-raised

`_fail()` (`connection.py:434`) and `_clean_reason()` (`connection.py:451`)
exist because the raw exceptions are unusable in a report:

* a device's login banner/MOTD is verbose and, across a wall of dead switches,
  drowns the report — so it goes to the **log file**, never into the report
  (`test_fail_keeps_banner_out_of_report_but_logs_it`);
* netmiko's `ReadTimeout` is a multi-paragraph "Things you might try…" blob —
  `_clean_reason` keeps only the first non-empty line;
* **the presence of captured bytes is itself a diagnosis.** If the session log
  holds anything, the SSH transport already succeeded, so this is a prompt or
  login problem, *not* ciphers. In that case a short hint is appended:
  *"reached the device but got no CLI prompt — on ERS this is the 'Ctrl-Y to
  begin' login gate or a slow box; raw output in the log"*. If the device sent
  nothing, no hint is added, because then it really may be an algorithm
  mismatch (`test_fail_no_hint_and_no_banner_when_device_silent`).

### Where each outcome lands

| Outcome | `SwitchAudit` | Console | Manifest | Health check | Exit code |
|---|---|---|---|---|---|
| connect failed | `reachable=False`, `errors[]` | `UNREACHABLE <name>` | `reachable: false` | `BLOCK` finding "the switch could not be read" | 1 |
| `enable` failed | `warnings[]` (via `setup_warnings`) | per-switch warning | warnings | — | 0 |
| paging-disable rejected | `warnings[]` (via `setup_warnings`) | per-switch warning | warnings | — | 0 |
| required command failed | `errors[]` | Issues | per-command `ok: false` | data-completeness findings | 1 |
| optional command failed | `warnings[]` | Issues | per-command `ok: false` | — | 0 |
| `absent_ok` command failed | *nothing* | — | per-command `ok: false` | — | 0 |
| session abandoned | `errors[]` per remaining command | Issues | commands marked failed | — | 1 |

`setup_warnings` reach the audit through one line in
`collectors/switch.py:37`:

```python
audit.warnings.extend(getattr(runner, "setup_warnings", []))
```

The `getattr` default is what lets any runner — including a test double —
satisfy the contract without declaring the attribute.

### The `absent_ok` discipline

`_run()` in `collectors/switch.py:59` takes two orthogonal flags:

| `required` | `absent_ok` | Failure becomes | Use for |
|---|---|---|---|
| `True` | — | `errors[]` | data the audit cannot do without (`show mlt`, `show vlan`) |
| `False` | `False` | `warnings[]` | optional data whose absence is worth knowing |
| `False` | `True` | log line only | commands that legitimately do not exist on every model/release |

`absent_ok` is what keeps the fallback dance out of the report: trying
`show lldp neighbor summary` on an ERS *always* fails, and reporting that as a
finding would train operators to ignore findings. Get this flag wrong in
either direction and the report becomes either noisy or dishonest.

---

## 9. Configuration reference (`ssh:` in `config.yaml`)

Defined in `SshSettings` (`config.py:40`), parsed in `load_config()`.

| Key | Default | Effect | Raise / set it when |
|---|---|---|---|
| `conn_timeout` | 20 | SSH establishment seconds (also floors banner/auth timeouts at 15) | WAN paths, slow boxes |
| `read_timeout` | 60 | per-command read ceiling | huge outputs; DvR sessions already get ≥120 automatically |
| `workers` | 4 | parallel device sessions (`ThreadPoolExecutor`) | large inventories; lower it if the mgmt network complains |
| `retries` | 1 | reconnect attempts after a **connect** failure | flaky reachability. Never applies to auth failures |
| `command_retries` | 1 | re-sends of one command that died on **transport** | a link that drops reads. Never applies to device rejections |
| `max_transport_failures` | 2 | consecutive transport failures → abandon session | lower = fail faster on dead boxes |
| `legacy_algorithms` | `true` | append SHA-1 kex / CBC / `ssh-rsa`+`ssh-dss` | leave on; it is free for modern devices |
| `global_delay_factor` | 1.0 | multiply every read wait | stubborn slow ERS only — it slows the whole run |
| `default_enter` | unset | line terminator, e.g. `"\r\n"` | ERS/BOSS that ignore bare `\n` |
| `disabled_algorithms` | unset | paramiko pin-*out*, keys `kex`/`ciphers`/`keys` | garbled channel on a modern algorithm |

`disabled_algorithms` is validated as a mapping at load time and rejected with
a message showing the expected shape — a typo there would otherwise surface as
an opaque paramiko error per device.

### Credentials

Never in a config file. `get_credentials()` (`config.py`) resolves in order:

1. `SM_USERNAME` / `SM_PASSWORD` (switches), `SM_DVR_USERNAME` /
   `SM_DVR_PASSWORD` (DvR controllers);
2. a fallback (DvR sessions reuse the switch credentials when no DvR-specific
   pair is set);
3. an interactive prompt — and if there is no TTY, a `ConfigError` that names
   the environment variables instead of hanging.

A read-only account is sufficient and recommended: every command the tool sends
is a `show` or a terminal-paging setting, and `--dry-run` proves it from the
real code path.

The manifest redacts any argparse option whose *name* matches
`pass|secret|token|credential|auth|key` (`manifest.py:33`), even though
credentials do not reach argparse today — a manifest is meant to be handed to
other people, and "it can't happen" is a bad bet to build an artifact on.

---

## 10. Troubleshooting decision tree

Start from the message in the console or `switch-migrator.log`.

```
"Incompatible ssh peer (no acceptable kex / host key algorithm)"
└─ transport never came up. Ciphers ARE the problem.
   1. is ssh.legacy_algorithms true?
   2. is paramiko 3.x? (paramiko 4 dropped ssh-dss)  ->  pip show paramiko
   3. check the log for "this paramiko build does not implement ..."

"ReadTimeout: Pattern not detected: '(?:\#|>)'"  (ERS)
└─ transport came up fine. Ciphers are NOT the problem.
   Look for the "[name] device output during failed connect" WARNING in the log:
   ├─ shows "Enter Ctrl-Y to begin" -> the login gate; PatientExtremeErs should
   │  handle it. If it still fails: ssh.default_enter: "\r\n"
   ├─ shows a Menu UI -> cmd-interface menu; fix on the device, or audit by hand
   ├─ shows a banner but no prompt -> slow CPU; ssh.global_delay_factor: 2
   └─ shows garbage/mojibake -> ssh.disabled_algorithms (pin out the modern one)

"authentication failed"
└─ NOT retried, by design (lockout avoidance). Check SM_USERNAME/SM_PASSWORD,
   and whether the account exists on THIS box.

"% Invalid input detected" on many commands of one VOSS switch
└─ the session is probably still at user EXEC ('>').
   Check the warning "could not enter privileged EXEC via 'enable'".
   Cause is the account's access level, not the release.

Long outputs stall / the NEXT command's output looks wrong
└─ the pager is alive. Check the warning "device rejected 'terminal more
   disable'". After any timeout the tool sends 'q' to kill a stuck --More--,
   but a device that refuses the paging command will keep doing this.

One device produced almost no data, others are fine
└─ circuit breaker: "abandoning session after N consecutive transport
   failures". The box stopped answering mid-audit. Partial data is kept.
   Re-run just that switch with -s name:platform:host.

A command failed and you want to see exactly what the device said
└─ --save-raw, then read output/raw/<device>/<command_slug>.txt,
   or replay the whole thing with --offline output/raw.
```

---

## 11. Recipes

**Prove the tool is read-only (change board / CAB):**

```bash
switch-migrator -i switches.yaml --dry-run
```
Prints every command that *could* be sent, per device, generated by the real
collectors against a runner that connects to nothing.

**Capture a device once, then work offline:**

```bash
switch-migrator -i switches.yaml --save-raw          # writes output/raw/<device>/*.txt
switch-migrator -i switches.yaml --offline output/raw   # replays, no SSH at all
```

**Turn a capture into a regression test:**

```bash
cp output/raw/old-access-07/show_mlt.txt tests/fixtures/ers/show_mlt.txt
```
Then assert against the parser directly, or build an `OfflineRunner` over a
`tmp_path` laid out as `<device>/<command_slug>.txt` — which is what every
collector test in `tests/` already does.

**Debug one stubborn box with full transport logging:**

```bash
switch-migrator -s old-access-07:ers:10.1.1.17 --debug
```
`--debug` lowers the stderr handler to DEBUG and stops silencing the `paramiko`
and `netmiko` loggers (`setup_logging`, `cli.py:189`).

---

## 12. Changing things

### 12a. Add a command for an existing platform

1. Add the `_run(...)` call in `_collect_voss` / `_collect_ers` (or
   `_enrich_migration_fields`) with the right `required` / `absent_ok`
   combination — see §8.
2. Write the parser in the matching `parsers/*.py`; **anchor on tokens, not
   column offsets** (see §13).
3. Add a fixture under `tests/fixtures/<platform>/<command_slug>.txt` from a
   real capture, and a parser test.
4. Update the command table in `README.md` **and** the per-platform table in
   §6/§7 here, including the per-device command count.
5. Run `--dry-run` and confirm the new command appears where you expect.

### 12b. Add a third platform end to end

In dependency order. Steps 1–5 are the connection layer; the rest is what a
half-done platform leaves broken.

1. `models.py`: add the enum member. `config.py:_make_target` accepts it
   automatically, but its **error message** hardcodes `'voss' or 'ers'` — fix
   that string.
2. `connection.py:NETMIKO_DEVICE_TYPE`: add the netmiko driver.
3. `connection.py:_PAGING_DISABLE`: add the paging command tuple. Missing
   entries here raise `KeyError` in both `SshRunner` and `DryRunRunner`.
4. If the platform has a login ritual: write a patient subclass beside
   `_patient_ers_class()` and select it in `_connect()`. Keep the
   "returns `None` → fall back to stock" degradation.
5. Verify `_ensure_privileged` applies (is `enable` a plain mode switch?) and
   whether `looks_like_error`'s markers cover this CLI's rejection wording.
6. `collectors/switch.py:38`: **convert the `if VOSS / else ERS` into explicit
   dispatch** and add `_collect_<platform>`. Do the same at
   `_collect_fdb` (`:219`), which is also written as "not VOSS → ERS".
7. `parsers/<platform>_parsers.py`, reusing `parsers/common.py`
   (`expand_port_list`, `parse_mac_table`, `normalize_mac`, LLDP parsers) —
   those are already cross-platform.
8. Decide the LLDP command order for it (`_enrich`, `:385`).
9. `compare.py`: does it have an I-SID concept? The VOSS-only branches at
   `:74` and `:129` decide `LOCAL_ONLY` vs `MISSING_ON_DVR`.
10. `config_extract` / `config_generate` / `report/migration.py:205`: what does
    "migrate this device's config" mean for it?
11. `switches.example.yaml` and `README.md`: document the new `platform:` value.
12. Tests: fixtures + a collector test + an end-to-end test mirroring
    `tests/test_end_to_end.py`.

### 12c. Change retry, timeout or breaker behaviour

Change `SshSettings` **and** `load_config()` **and** `config.example.yaml`
together — the defaults are duplicated between the dataclass and the loader's
`.get(...)` calls, so editing only one leaves the YAML default silently
disagreeing with the code default.

---

## 13. Invariants — do not break these

Each of these exists because of a real failure, and most are pinned by a test.

1. **Never retry authentication failures.** Account lockout across an entire
   inventory. `_connect` re-raises `NetmikoAuthenticationException` immediately.
2. **Never retry a command the device rejected.** The answer will not change,
   and the retry hides a real release/privilege finding behind latency.
3. **Append legacy algorithms, never prepend, never replace.** Prepending would
   downgrade healthy VOSS and DvR sessions to SHA-1/CBC.
4. **Keep `paramiko>=3.4,<4`** until the last `ssh-dss` box is gone. Guarded by
   a test that reads the requirement files.
5. **Keep `fast_cli=False`.** Old ERS gear chokes on netmiko's fast path.
6. **Never send anything but `show`, `enable` and the paging command.** The
   read-only claim is the tool's licence to run in a change window, and
   `--dry-run` publishes the full list.
7. **Never record writes in the session log** (`session_log_record_writes=False`)
   — the password is a write.
8. **Never put a device banner in the report.** Log it; put a one-line
   actionable hint in the error.
9. **One dead device must never abort the run.** `audit_one_switch` catches
   `ConnectionFailed`, then bare `Exception`, then wraps collection in another
   bare `Exception` handler, and closes in `finally`.
10. **Parsers anchor on tokens, never on column offsets** (the one deliberate
    exception is `parse_lldp_neighbors_summary`, which derives offsets *from
    the header line at runtime* — precisely so an empty SYSNAME cell cannot be
    filled by the neighbouring column's text).
11. **`enable_legacy_ssh_algorithms()` mutates process-global paramiko state.**
    It is guarded by a lock and an idempotence flag; runners are built from a
    thread pool. Do not add more global mutation to this layer.
12. **Each thread gets its own runner and its own `FabricState`;** merging
    happens afterwards via `FabricState.merge()`. `collect_dvr` is documented
    as thread-unsafe by design.
13. **A DvR controller is authoritative only if the fabric-wide I-SID list was
    read.** Otherwise partial data would produce false `MISSING_ON_DVR`
    verdicts.

---

## 14. Test map

`tests/test_connection.py` is the executable specification of this layer:

| Area | Tests |
|---|---|
| error detection | `test_looks_like_error_plain`, `..._behind_banner`, `..._negative` |
| paging | `test_paging_disable_verified_ok`, `..._falls_back_to_abbreviated_form`, `..._rejection_is_surfaced` |
| privilege | `test_already_privileged_prompt_skips_enable`, `test_unprivileged_prompt_triggers_enable`, `test_enable_failure_is_a_visible_warning` |
| resilience | `test_stuck_pager_is_quit_after_transport_failure`, `test_circuit_breaker_abandons_dead_session`, `test_circuit_breaker_resets_on_success` |
| legacy SSH | `test_enable_legacy_ssh_algorithms`, `..._appended_not_prepended`, `..._is_idempotent`, `test_paramiko_pin_still_excludes_the_release_that_dropped_ssh_dss`, `test_installed_paramiko_supports_ers_host_keys` |
| ERS login | `test_patient_ers_special_login_presses_ctrl_y`, `test_patient_ers_class_retries_then_succeeds`, `..._gives_up_and_reraises` |
| error messages | `test_fail_keeps_banner_out_of_report_but_logs_it`, `test_fail_no_hint_and_no_banner_when_device_silent`, `test_clean_reason_takes_only_the_first_line` |

The pattern for testing `SshRunner` without a device is
`object.__new__(SshRunner)` plus a `_FakeConn` exposing `send_command`,
`find_prompt`, `enable`, `write_channel`, `clear_buffer` — the runner is never
constructed, so `__init__`'s connect is never reached. Reuse `_make_runner()`
rather than inventing a second double.

Everything else in `tests/` uses `OfflineRunner` against fixtures, so the whole
suite runs with no network and no device.

```bash
pip install -e '.[dev]'
pytest -v
pytest -v tests/test_connection.py     # this layer only
```
