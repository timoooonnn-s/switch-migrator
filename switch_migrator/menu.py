"""Interactive menu - the toolkit front end.

Running `switch-migrator` with no arguments drops you here. The menu owns a
SESSION: devices are collected ONCE and every output (audit report, inventory
report, migration sheets, config extract) is produced from that same data, so
switching between use cases costs no further SSH round-trips.

All command-line flags keep working unchanged; this is an additional entry
point, not a replacement.
"""

from __future__ import annotations

import glob
import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable

from switch_migrator import cabling_sheet, connection, mac_check, mlt_generate, verify
from switch_migrator import handover as handover_mod
from switch_migrator import health as health_mod
from switch_migrator import location as location_mod
from switch_migrator import manifest as manifest_mod
from switch_migrator import profiles as profiles_mod
from switch_migrator import snapshot as snapshot_mod
from switch_migrator.collectors.switch import Reads
from switch_migrator.config import (
    Config,
    ConfigError,
    Credentials,
    SwitchTarget,
    default_inventory,
    get_credentials,
    load_config,
    load_inventory,
    parse_switch_arg,
)
from switch_migrator.connection import CommandEntry, enable_legacy_ssh_algorithms
from switch_migrator.location import LocationRules
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator.plan import Layout, pins_from_sheet, uids_from_sheet
from switch_migrator.report import console as console_report
from switch_migrator.report.excel import write_excel
from switch_migrator.report.migration import new_switch_names
from switch_migrator.report.progress import make_progress
from switch_migrator.workflow.collect import (
    CollectContext,
    CollectOptions,
    collect,
    make_runner,
    preview_commands,
    render_dry_run,
)
from switch_migrator.workflow.macs import after_recorder, run_mac_check
from switch_migrator.workflow.outputs import OutputOptions, write_outputs, write_rack_sheet
from switch_migrator.workflow.report import PlanSetup, ReportOptions, build_report

# blank answers in a row after which a closed stdin is taken as 'quit'
MAX_IDLE_ROUNDS = 50


@dataclass
class Session:
    """Everything the menu knows; collected data is reused across actions."""
    config_path: Path
    cfg: Config
    inventory_path: Path | None = None
    all_targets: list[SwitchTarget] = field(default_factory=list)
    selected: list[SwitchTarget] = field(default_factory=list)
    output_dir: Path = Path("output")
    offline_dir: Path | None = None
    save_raw: bool = False
    write_manifest: bool = False
    # crash insurance: persist every collection to a timestamped snapshot
    # right away, so nothing ever has to be re-collected after a crash
    auto_snapshot: bool = True
    split_by_location: bool = False
    location_groups: dict[str, list[str]] = field(default_factory=dict)
    new_switch: str = ""
    profiles_path: Path = field(
        default_factory=lambda: profiles_mod.DEFAULT_FILE)
    # collected data
    audits: list[SwitchAudit] = field(default_factory=list)
    fabric: FabricState | None = None
    collected_at: datetime | None = None
    collected_fabric: bool = False
    collected_config: bool = False
    collected_macs: bool = False
    # provenance: set when the data came from a snapshot rather than devices
    loaded_from: Path | None = None
    commands_by_device: dict[str, list[CommandEntry]] = field(default_factory=dict)
    # the last migration-check MAC snapshot taken in this session (the
    # baseline the verification offers by default)
    mac_snapshot: Path | None = None

    @property
    def has_data(self) -> bool:
        return bool(self.audits)

    def location_rules(self) -> LocationRules:
        """Config rules, with this session's per-run grouping when set."""
        if not self.location_groups:
            return self.cfg.locations
        return _replace(self.cfg.locations, groups=self.location_groups)

    def collect_options(self, *, fabric: bool = False, extract_config: bool = False,
                        migration_sheets: bool = False,
                        verify: bool = False) -> CollectOptions:
        """How this session reaches the devices (offline replay, raw capture),
        plus what one collection reads - the same type the CLI builds."""
        return CollectOptions(
            reads=Reads.for_run(extract_config=extract_config,
                                migration_sheets=migration_sheets, verify=verify),
            fabric=fabric, offline=self.offline_dir,
            raw_dir=(self.output_dir / "raw") if self.save_raw else None)

    def run_options(self, **what: object) -> dict[str, object]:
        """What a run was asked to do, for the manifest."""
        return {"inventory": self.inventory_path, "output_dir": self.output_dir,
                "offline": self.offline_dir, "save_raw": self.save_raw,
                "new_switch": self.new_switch, **what}


# asks for (switch, DvR) credentials; (None, None) when the user backs out
CredsFn = Callable[["Session", Console], tuple["Credentials | None", "Credentials | None"]]


class Abort(Exception):
    """The user typed 'x' (or pressed Ctrl-C) at a prompt: abandon the current
    action immediately and fall back to the main menu. Every prompt honours
    it, so any process can be aborted at any state.

    `interrupt` distinguishes Ctrl-C from a typed 'x'. Inside an action both
    mean the same thing - drop it and go back. At the main menu itself there is
    no action to drop, and Ctrl-C there means what it means in every other
    terminal program: quit.
    """

    def __init__(self, interrupt: bool = False):
        super().__init__()
        self.interrupt = interrupt


def _load_readline() -> ModuleType | None:
    """readline where the platform has it (not on plain Windows)."""
    try:
        return importlib.import_module("readline")
    except ImportError:
        return None


_READLINE = _load_readline()


@contextmanager
def _path_completion() -> Iterator[None]:
    """Readline tab-completion for filesystem paths, active only around a
    path prompt. Degrades to nothing where readline is unavailable."""
    readline = _READLINE
    if readline is None:                    # e.g. Windows without pyreadline3
        yield
        return

    def complete(text: str, state: int) -> str | None:
        expanded = os.path.expanduser(text)
        matches = sorted(glob.glob(expanded + "*"))
        matches = [m + os.sep if os.path.isdir(m) else m for m in matches]
        return matches[state] if state < len(matches) else None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    try:
        yield
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def _ask(console: Console, prompt: str, default: str = "",
         path: bool = False) -> str:
    """One prompt. Blank returns the default; 'x' aborts the whole action
    (Abort); EOF (closed stdin / exhausted test script) reads as blank.
    path=True turns on tab-completion for filesystem paths."""
    suffix = f" [{default}]" if default else ""
    try:
        if path:
            with _path_completion():
                raw = console.input(
                    f"[bold cyan]{prompt}{suffix}:[/bold cyan] ").strip()
        else:
            raw = console.input(
                f"[bold cyan]{prompt}{suffix}:[/bold cyan] ").strip()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        raise Abort(interrupt=True) from None
    if raw.lower() == "x":
        raise Abort()
    return raw or default


def _yes(console: Console, prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    ans = _ask(console, f"{prompt} ({d})").lower()
    return default if not ans else ans.startswith("y")


def _status_panel(s: Session) -> Panel:
    lines = [
        f"[bold]Config:[/bold]    {s.config_path}",
        f"[bold]Inventory:[/bold] {s.inventory_path or '(none - add switches manually)'}",
        f"[bold]Switches:[/bold]  {len(s.selected)} selected of {len(s.all_targets)}",
        f"[bold]Output:[/bold]    {s.output_dir}",
    ]
    if s.offline_dir:
        lines.append(f"[bold]Mode:[/bold]      "
                     f"[yellow]OFFLINE replay from {s.offline_dir}[/yellow]")
    if s.has_data:
        when = s.collected_at.strftime("%H:%M:%S") if s.collected_at else "?"
        extras = []
        if s.collected_fabric:
            extras.append("fabric")
        if s.collected_config:
            extras.append("running-config")
        if s.collected_macs:
            extras.append("MACs")
        source = f"loaded from {s.loaded_from.name}" if s.loaded_from \
            else f"collected {when}"
        lines.append(f"[bold green]Data:[/bold green]      {source} "
                     f"({len(s.audits)} switch(es)"
                     + (f", incl. {', '.join(extras)}" if extras else "") + ")")
    else:
        lines.append("[bold yellow]Data:[/bold yellow]      not collected yet")
    if s.mac_snapshot:
        lines.append(f"[bold]MAC base:[/bold]  {s.mac_snapshot}")
    return Panel("\n".join(lines), title="switch-migrator", title_align="left")


_NEEDS_DATA = ("3", "4", "5", "6", "7", "h", "b")

_ENTRIES = [
    ("1", "Select switches", "pick targets from the inventory or add them by hand"),
    ("2", "Collect from devices", "connect once; all outputs below reuse this data"),
    ("3", "Audit vs fabric", "compare every VLAN against the DvR fabric state"),
    ("4", "Inventory report", "port/MLT/VLAN state only, no fabric comparison"),
    ("5", "Migration sheets", "port info + DC cabling sheet + MAC-check commands"),
    ("6", "Config extract", "neutralized VOSS config / generated ERS->VOSS draft"),
    ("7", "Everything", "run 3-6 in one go with the collected data"),
    ("8", "Snapshot", "save this session's data, or load an earlier one"),
    ("9", "Dry run", "list every command a collection would send - connects to nothing"),
    ("h", "Health check", "go/no-go before the window: what is already broken?"),
    ("m", "Generate MLT blocks", "new switches' MLT config from a filled-in cabling sheet"),
    ("r", "Rack sheet", "print version of a filled-in cabling sheet for the technicians"),
    ("v", "Verify migration", "after the window: check every re-patched link"),
    ("c", "Compare MAC snapshots", "two saved MAC folders, by MAC - connects to nothing"),
    ("b", "Handover bundle", "one folder with every output and an index to hand over"),
    ("p", "Profiles", "load or save a named scenario (inventory + settings)"),
    ("s", "Settings", "change one setting at a time; nothing else is touched"),
    ("0", "Quit", ""),
]


def _menu_table(s: Session) -> RichTable:
    t = RichTable(show_header=False, box=None, pad_edge=False)
    t.add_column(justify="right", style="bold cyan", width=3)
    t.add_column(style="bold")
    t.add_column(style="dim")
    for key, label, hint in _ENTRIES:
        needs_data = key in _NEEDS_DATA and not s.has_data
        style = "dim" if needs_data else None
        suffix = "  (collect first)" if needs_data else ""
        t.add_row(key, label + suffix, hint, style=style)
    return t


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

def action_select(s: Session, console: Console) -> None:
    if not _have_inventory(s, console):
        return
    t = RichTable(title="Inventory")
    t.add_column("#", justify="right")
    t.add_column("Name")
    t.add_column("Platform")
    t.add_column("Host")
    for i, tgt in enumerate(s.all_targets, 1):
        t.add_row(str(i), tgt.name, tgt.platform.value, tgt.host)
    console.print(t)
    raw = _ask(console, "Select numbers (e.g. 1,3-5), 'all', or blank to keep", "all")
    if raw.lower() == "all":
        s.selected = list(s.all_targets)
    else:
        picked = _pick_targets(s.all_targets, raw, console)
        if picked:
            s.selected = picked
    console.print(f"[green]{len(s.selected)} switch(es) selected.[/green]")


def _have_inventory(s: Session, console: Console) -> bool:
    """Load or ask for the inventory. False when there is nothing to pick from
    - or the switches were entered by hand, which selects them all."""
    if s.inventory_path and not s.all_targets:
        try:
            s.all_targets = load_inventory(s.inventory_path)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
    if s.all_targets:
        return True
    path = _ask(console, "Inventory YAML path (blank to add switches by hand)",
                path=True)
    if not path:
        _enter_targets(s, console)
        return False
    try:
        s.inventory_path = Path(path)
        s.all_targets = load_inventory(s.inventory_path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return False
    return True


def _enter_targets(s: Session, console: Console) -> None:
    raw = _ask(console, "Switches as NAME:PLATFORM[:HOST], comma separated")
    targets = []
    for item in (x.strip() for x in raw.split(",") if x.strip()):
        try:
            targets.append(parse_switch_arg(item))
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
    s.all_targets = targets
    s.selected = list(targets)
    console.print(f"[green]{len(targets)} switch(es) set.[/green]")


def _pick_targets(targets: list[SwitchTarget], raw: str,
                  console: Console) -> list[SwitchTarget]:
    """'1,3-5' -> those inventory entries; anything unreadable is ignored, loudly."""
    picked: list[SwitchTarget] = []
    for part in (chunk.strip() for chunk in raw.split(",")):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                picked += targets[a - 1:b]
            else:
                picked.append(targets[int(part) - 1])
        except (ValueError, IndexError):
            console.print(f"[red]ignoring '{part}'[/red]")
    return picked


def _preflight_credentials(s: Session, console: Console,
                           creds: Credentials) -> Credentials | None:
    """One quick SSH login against one device BEFORE the full run, so wrong
    credentials fail in seconds instead of a read_timeout per device - and can
    be re-entered on the spot. Returns the (possibly corrected) credentials,
    or None to abort the collection."""

    if s.offline_dir is not None:
        return creds                    # replay: nothing to authenticate
    # console-routed switches authenticate differently; probe a plain one
    probe = next((t for t in s.selected if not getattr(t, "console", "")), None)
    if probe is None:
        return creds
    for _ in range(3):
        console.print(f"[dim]Checking credentials against {probe.name} "
                      f"({probe.host})...[/dim]")
        result = connection.quick_auth_check(probe.host, creds, s.cfg.ssh)
        if result is None:
            console.print("[green]Credentials OK.[/green]")
            return creds
        if result != "auth":
            # unreachable / algorithm trouble is not proof the creds are wrong
            console.print(f"[yellow]Could not verify credentials against "
                          f"{probe.name}: {result}[/yellow]")
            return creds if _yes(console, "Continue with the collection anyway?") else None
        console.print(f"[red]{probe.name} refused the credentials.[/red]")
        entered = _reenter_credentials(console, creds)
        if entered is None:
            return None
        creds = entered
    console.print("[red]Still refused after 3 attempts - aborting the "
                  "collection (no lockout risk taken).[/red]")
    return None


def _reenter_credentials(console: Console, creds: Credentials) -> Credentials | None:
    """Ask again after a refusal; None when the user gives up."""
    user = _ask(console, "Username", creds.username)
    try:
        pw = console.input("[bold cyan]Password:[/bold cyan] ", password=True)
    except (EOFError, KeyboardInterrupt):
        return None
    if not user or not pw:
        return None
    return Credentials(user, pw)


@dataclass
class _CollectChoices:
    """What the user said yes to before a collection."""
    fabric: bool
    config: bool
    macs: bool
    mac_snapshot: bool

    def run_options(self, s: Session) -> dict[str, object]:
        return s.run_options(fabric=self.fabric, extract_config=self.config,
                             migration_sheets=self.macs)


def _ask_collect_choices(s: Session, console: Console) -> _CollectChoices:
    want_fabric = bool(s.cfg.dvr_controllers) and _yes(
        console, f"Collect fabric state from {len(s.cfg.dvr_controllers)} DvR "
                 f"controller(s)? (needed for the audit comparison)")
    want_config = _yes(console, "Also pull running-config? (needed for config "
                                "extract; also gives the sheets tagged/untagged)")
    want_macs = _yes(console, "Also collect MAC tables? (needed for migration sheets)")
    # the migration-check baseline: taken on the same sessions, so it costs a
    # few extra show commands per switch and no extra login
    want_mac_snapshot = _yes(
        console, "Also save a MAC snapshot for the after-check? (the baseline "
                 "migration-check compares the new switches against)", want_macs)
    return _CollectChoices(want_fabric, want_config, want_macs, want_mac_snapshot)


def _collect_credentials(s: Session, console: Console, creds_fn: CredsFn
                         ) -> tuple[Credentials, Credentials] | None:
    """(switch, DvR) credentials, checked against one switch; None to abort."""
    creds, dvr_creds = creds_fn(s, console)
    if creds is None:
        return None
    checked = _preflight_credentials(s, console, creds)
    if checked is None:
        return None
    if dvr_creds is None or dvr_creds is creds:
        # no separate DvR account (the CLI's SM_DVR_* falls back the same way),
        # or the DvR sessions reuse the switch credentials: keep them in step
        # when the pre-flight corrected those
        dvr_creds = checked
    return checked, dvr_creds


def action_collect(s: Session, console: Console, creds_fn: CredsFn) -> None:
    if not s.selected:
        console.print("[yellow]No switches selected - use option 1 first.[/yellow]")
        return
    want = _ask_collect_choices(s, console)
    both = _collect_credentials(s, console, creds_fn)
    if both is None:
        return
    creds, dvr_creds = both

    opts = s.collect_options(fabric=want.fabric, extract_config=want.config,
                             migration_sheets=want.macs)
    started = datetime.now()
    recorder = None
    if want.mac_snapshot:
        path = s.output_dir / f"mac-snapshot-{started:%Y%m%d-%H%M%S}"
        try:
            recorder = mac_check.SnapshotRecorder(path, label=path.name)
        except Exception as exc:  # broad on purpose: the collection itself still runs
            console.print(f"[yellow]MAC snapshot skipped: {exc}[/yellow]")
    with make_progress(console, len(s.selected),
                       len(s.cfg.dvr_controllers) if want.fabric else 0) as prog:
        collected = collect(s.selected, creds, dvr_creds,
                            CollectContext(s.cfg, opts, prog, recorder))
    if recorder is not None:
        s.mac_snapshot = recorder.path
        console.print(f"[green]MAC snapshot:[/green] {s.mac_snapshot} "
                      f"[dim](offered as the baseline in 'v')[/dim]")
    if want.fabric and not _fabric_read(collected.fabric, console):
        want.fabric = False

    s.audits, s.fabric = collected.audits, collected.fabric
    s.collected_at = datetime.now()
    s.collected_fabric = want.fabric
    # The migration-sheet collection reads the running-config too, for the
    # tagging - but drops the text again unless it was asked for. So what is
    # RETAINED, which is what the config extract and the snapshot need, is
    # still exactly what the user said yes to here.
    s.collected_config = want.config
    s.collected_macs = want.macs
    s.loaded_from = None
    s.commands_by_device = collected.commands
    _report_collection(s, console)
    if s.auto_snapshot and s.audits:
        _auto_snapshot(s, started, console)
    if s.write_manifest:
        s.output_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_mod.write(
            s.output_dir / f"manifest-{started.strftime('%Y%m%d-%H%M%S')}.json",
            manifest_mod.build(collected.audits, collected.fabric, manifest_mod.RunRecord(
                want.run_options(s), started, datetime.now(),
                commands=collected.commands, config_path=s.config_path)))
        console.print(f"[green]Run manifest:[/green] {path}")


def _fabric_read(fabric: FabricState, console: Console) -> bool:
    """True when at least one DvR controller delivered the fabric state."""
    if not fabric.dvrs_ok:
        console.print("[red]No DvR controller could be read - continuing "
                      "without fabric data (audit comparison unavailable).[/red]")
        return False
    console.print(f"[green]Fabric: {len(fabric.isids)} I-SIDs from "
                  f"{', '.join(fabric.dvrs_ok)}[/green]")
    return True


def _report_collection(s: Session, console: Console) -> None:
    ok = sum(1 for a in s.audits if a.reachable)
    console.print(f"[green]Collected {ok}/{len(s.audits)} switch(es).[/green]")
    for a in s.audits:
        if not a.reachable:
            console.print(f"  [red]UNREACHABLE {a.name}: "
                          f"{a.errors[-1] if a.errors else 'unknown'}[/red]")


def _auto_snapshot(s: Session, started: datetime, console: Console) -> None:
    """Crash insurance: the expensive part is now on disk, timestamped;
    option 8 lists these for one-keystroke reload."""
    path = (s.output_dir / "snapshots"
            / f"snapshot-{started.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        snapshot_mod.save(path, s.audits, s.fabric or FabricState(),
                          meta=_snapshot_meta(s))
        console.print(f"[dim]Auto-saved snapshot: {path}[/dim]")
    except OSError as exc:
        console.print(f"[yellow]could not auto-save a snapshot: {exc}[/yellow]")


@dataclass(frozen=True)
class _UseCase:
    """What one menu report writes."""
    title: str
    no_fabric: bool = True
    extract_config: bool = False
    migration_sheets: bool = False
    plan: PlanSetup | None = None


def _write_outputs(s: Session, console: Console, use: _UseCase) -> None:
    """One use case on the session's data: the report, shown and written."""
    split = (s.location_rules() if use.migration_sheets and s.split_by_location
             else None)
    report = build_report(
        s.audits, s.fabric or FabricState(), s.cfg,
        ReportOptions(fabric_checked=not use.no_fabric,
                      migration_sheets=use.migration_sheets,
                      new_switches=new_switch_names(s.new_switch), plan=use.plan,
                      split=split),
        console)
    console_report.render(report.tables, console, verbose=False)
    s.output_dir.mkdir(parents=True, exist_ok=True)
    written = write_outputs(
        report, s.audits, s.cfg,
        OutputOptions(output_dir=s.output_dir,
                      stamp=datetime.now().strftime("%Y%m%d-%H%M%S"), title=use.title,
                      extract_config=use.extract_config, commands=use.migration_sheets,
                      new_switch=s.new_switch),
        console)
    for path in written:
        console.print(f"[green]Written:[/green] {path}")


def action_audit(s: Session, console: Console) -> None:
    if not s.collected_fabric:
        console.print("[yellow]No fabric data in this session - the audit needs "
                      "it. Re-collect (option 2) and answer yes to the fabric "
                      "question, or use the inventory report (option 4).[/yellow]")
        return
    _write_outputs(s, console, _UseCase("migration-audit", no_fabric=False))


def action_inventory(s: Session, console: Console) -> None:
    _write_outputs(s, console, _UseCase("inventory-report"))


def action_sheets(s: Session, console: Console) -> None:
    if not s.collected_macs:
        console.print("[yellow]No MAC data in this session - the sheets will "
                      "have empty MAC columns. Re-collect (option 2) and answer "
                      "yes to the MAC question for full sheets.[/yellow]")
        if not _yes(console, "Continue anyway?", default=False):
            return
    s.new_switch = _ask(console, "NEW switch(es), comma-separated (A,B = one vIST "
                                 "pair, A,B,C,D = two)", default=s.new_switch)
    plan = _ask_plan(s, console) if s.new_switch else None
    _write_outputs(s, console, _UseCase("migration-sheets",
                                        no_fabric=not s.collected_fabric,
                                        migration_sheets=True, plan=plan))


def _ask_plan(s: Session, console: Console) -> PlanSetup | None:
    """Plan the new ports? The answers become this session's defaults."""

    if not _yes(console, "Plan where every link goes (pre-fill NEW switch/port)?",
                default=True):
        return None
    while True:
        ports = _ask(console, "Ports on the new switch model",
                     default=str(s.cfg.plan.ports or ""))
        uplinks = _ask(console, "How many of the LAST ports are uplinks",
                       default=str(s.cfg.plan.uplinks if s.cfg.plan.uplinks is not None
                                   else ""))
        if not ports or not uplinks:
            console.print("[yellow]No layout given - sheets without a plan.[/yellow]")
            return None
        try:
            layout = Layout(ports=int(ports), uplinks=int(uplinks))
            break
        except ValueError as exc:
            console.print(f"[red]{exc or 'enter two numbers'}[/red]")
    s.cfg.plan.ports, s.cfg.plan.uplinks = layout.ports, layout.uplinks
    raw = _ask(console, "Keep the NEW ports of an edited cabling sheet? (path, "
                        "blank: plan from scratch)", path=True)
    if raw:
        try:
            rows = cabling_sheet.load(Path(raw)).rows
        except cabling_sheet.SheetError as exc:
            console.print(f"[yellow]{exc} - planning from scratch[/yellow]")
        else:
            return PlanSetup(layout, pins_from_sheet(rows), uids_from_sheet(rows))
    return PlanSetup(layout)


def action_config(s: Session, console: Console) -> None:
    if not s.collected_config:
        console.print("[yellow]No running-config in this session. Re-collect "
                      "(option 2) and answer yes to the running-config "
                      "question.[/yellow]")
        return
    _write_outputs(s, console, _UseCase("config-extract",
                                        no_fabric=not s.collected_fabric,
                                        extract_config=True))


def action_everything(s: Session, console: Console) -> None:
    if not s.new_switch:
        s.new_switch = _ask(console, "Name of the NEW switch (for the commands file)")
    _write_outputs(s, console, _UseCase("migration-full",
                                        no_fabric=not s.collected_fabric,
                                        extract_config=s.collected_config,
                                        migration_sheets=True))


def _snapshot_meta(s: Session) -> dict[str, Any]:
    return {"collected_at": s.collected_at.isoformat(timespec="seconds")
            if s.collected_at else "",
            "config": str(s.config_path),
            "no_fabric": not s.collected_fabric,
            "has_running_config": s.collected_config,
            "has_macs": s.collected_macs}


def _find_snapshots(s: Session) -> list[Path]:
    """Snapshot files this session's output would hold, newest first - the
    timestamped names sort chronologically."""
    found: set[Path] = set()
    for where in (s.output_dir, s.output_dir / "snapshots"):
        if where.is_dir():
            found.update(p for p in where.glob("snapshot-*.json") if p.is_file())
    return sorted(found, key=lambda p: p.name, reverse=True)


def action_snapshot(s: Session, console: Console) -> None:
    """Save the session's collected state, or load an earlier one.

    A snapshot makes the collection reusable: the expensive, credential-bound,
    device-touching part happens once and every report can be rebuilt from the
    file afterwards - next week, or by a colleague who cannot reach the boxes.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if s.has_data:
        console.print("[bold]1[/bold] save this session   "
                      "[bold]2[/bold] load a snapshot (replaces the session data)")
        choice = _ask(console, "Choose", "1")
    else:
        console.print("[dim]Nothing collected yet - loading a snapshot.[/dim]")
        choice = "2"

    if choice == "1":
        default = str(s.output_dir / "snapshots" / f"snapshot-{stamp}.json")
        path = Path(_ask(console, "Snapshot file", default, path=True))
        try:
            written = snapshot_mod.save(path, s.audits,
                                        s.fabric or FabricState(),
                                        meta=_snapshot_meta(s))
        except OSError as exc:
            console.print(f"[red]could not write {path}: {exc}[/red]")
            return
        console.print(f"[green]Snapshot written:[/green] {written}")
        console.print("[dim]It holds device data (hostnames, IPs, MACs, "
                      "neighbors) - keep it where the switch output belongs.[/dim]")
        return

    found = _find_snapshots(s)
    if found:
        t = RichTable(title="Snapshots found", title_justify="left")
        t.add_column("#", justify="right")
        t.add_column("File")
        t.add_column("Size", justify="right")
        for i, p in enumerate(found[:15], 1):
            t.add_row(str(i), str(p), f"{p.stat().st_size // 1024} KB")
        console.print(t)
    raw = _ask(console, "Snapshot to load (number or file path)", path=True)
    if not raw:
        return
    if raw.isdigit() and found and 1 <= int(raw) <= len(found[:15]):
        path = found[int(raw) - 1]
    else:
        path = Path(raw)
    try:
        audits, fabric, meta = snapshot_mod.load(path)
    except snapshot_mod.SnapshotError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    s.audits, s.fabric = audits, fabric
    s.loaded_from = path
    s.collected_at = None
    s.commands_by_device = {}
    s.collected_fabric = bool(fabric.dvrs_ok) and not meta.get("no_fabric")
    s.collected_config = any(a.running_config for a in audits)
    s.collected_macs = any(p.macs or p.usage for a in audits for p in a.ports)
    console.print(f"[green]Loaded:[/green] {snapshot_mod.describe(meta, audits)}")


def action_dry_run(s: Session, console: Console) -> None:
    if not s.selected:
        console.print("[yellow]No switches selected - use option 1 first.[/yellow]")
        return
    want_config = _yes(console, "Include the running-config pull?", False)
    want_macs = _yes(console, "Include the MAC/optics collection?", True)
    want_mac_snapshot = _yes(
        console, "Include the MAC snapshot for migration-check?", False)
    render_dry_run(preview_commands(
        s.selected, s.cfg,
        s.collect_options(extract_config=want_config, migration_sheets=want_macs),
        dvrs=s.cfg.dvr_controllers,
        mac_for={t.name for t in s.selected} if want_mac_snapshot else ()), console)


def action_health(s: Session, console: Console) -> None:
    """Go/no-go on the collected data. Sends nothing to any device."""
    report = build_report(s.audits, s.fabric or FabricState(), s.cfg,
                          ReportOptions(fabric_checked=s.collected_fabric, health=True))
    tables = report.sections["health"]
    console_report.render(tables, console, verbose=False)

    health = report.health
    assert health is not None          # asked for with health=True
    counts = health.counts()
    color = {health_mod.BLOCK: "bold red", health_mod.WARN: "bold yellow",
             health_mod.OK: "bold green"}.get(health.verdict, "bold")
    console.print(f"[{color}]Pre-migration health: {health.verdict}[/{color}] - "
                  f"{counts[health_mod.BLOCK]} blocked, "
                  f"{counts[health_mod.WARN]} with warnings, "
                  f"{counts[health_mod.OK]} clean")
    if not s.collected_fabric:
        console.print("[dim]No fabric data in this session - VLANs that have "
                      "nowhere to land in the fabric were not checked.[/dim]")
    s.output_dir.mkdir(parents=True, exist_ok=True)
    path = s.output_dir / f"health-check-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    write_excel(tables, path)
    console.print(f"[green]Written:[/green] {path}")


def action_generate_mlt(s: Session, console: Console) -> None:
    """Cabling sheet in, MLT config blocks out. Touches no device."""

    raw = _ask(console, "Filled-in cabling sheet (.xlsx or .csv)", path=True)
    if not raw:
        return
    path = Path(raw)
    try:
        sheet = cabling_sheet.load(path)
    except cabling_sheet.SheetError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    smlt = _yes(console, "SMLT pairs (same MLT id on both vIST peers)?", True)
    result = mlt_generate.plan(sheet, smlt=smlt)
    for problem in result.problems:
        console.print(f"[yellow]![/yellow] {problem}")
    out = s.output_dir / "config" / "mlt-blocks.cfg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(mlt_generate.render(result, source=str(path)),
                   encoding="utf-8")
    console.print(f"[green]{len(result.plans)} MLT(s)[/green] from "
                  f"{len(sheet.migrated)} re-patched link(s)")
    for plan in result.plans:
        console.print(f"  {plan.switch}  MLT {plan.mlt_id} \"{plan.name}\"  "
                      f"members {','.join(plan.members)}")
    console.print(f"[green]Written:[/green] {out}")


def action_rack_sheet(s: Session, console: Console) -> None:
    """Cabling sheet in, printable rack sheet out. Touches no device."""

    raw = _ask(console, "Filled-in cabling sheet (.xlsx or .csv)", path=True)
    if not raw:
        return
    try:
        sheet = cabling_sheet.load(Path(raw))
    except cabling_sheet.SheetError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    path = write_rack_sheet(sheet, s.output_dir)
    console.print(f"[green]Rack sheet[/green] ({len(sheet.migrated)} of "
                  f"{len(sheet.rows)} link(s) planned): {path}")


def action_verify(s: Session, console: Console, creds_fn: CredsFn) -> None:
    """Read the filled-in sheet, collect the NEW switches, check every link."""

    raw = _ask(console, "Filled-in cabling sheet (.xlsx or .csv)", path=True)
    if not raw:
        return
    path = Path(raw)
    try:
        sheet = cabling_sheet.load(path)
    except cabling_sheet.SheetError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    names = sorted(sheet.by_new_switch())
    if not names:
        console.print("[yellow]No row has both a NEW switch and a NEW port "
                      "yet - nothing to verify.[/yellow]")
        return
    console.print(f"The sheet names {len(names)} new switch(es): "
                  f"{', '.join(names)}")
    targets = [SwitchTarget(name=n, host=n, platform=Platform.VOSS)
               for n in names]
    baselines, watching = _ask_mac_baselines(s, console)

    creds, _ = creds_fn(s, console)
    if creds is None:
        return
    # the MACs on the new ports are the evidence, so collect them
    opts = s.collect_options(verify=True)
    started = datetime.now()
    stamp = f"{started:%Y%m%d-%H%M%S}"
    recorder = (after_recorder(s.output_dir, stamp, watching, targets, sheet)
                if baselines else None)
    with make_progress(console, len(targets)) as prog:
        audits = collect(targets, creds, creds,
                         CollectContext(s.cfg, opts, prog, recorder)).audits

    built = build_report(audits, FabricState(), s.cfg, ReportOptions(verify_sheet=sheet))
    report, tables = built.verify, built.sections["verify"]
    assert report is not None          # asked for with verify_sheet
    console_report.render(tables, console, verbose=False)
    counts = report.counts()
    color = "bold red" if counts[verify.FAIL] else (
        "bold yellow" if counts[verify.WARN] else "bold green")
    console.print(f"[{color}]Verification:[/{color}] {counts[verify.PASS]} pass, "
                  f"{counts[verify.WARN]} warn, {counts[verify.FAIL]} fail, "
                  f"{counts[verify.PENDING]} not migrated yet")
    for problem in report.problems:
        console.print(f"  [yellow]{problem}[/yellow]")
    s.output_dir.mkdir(parents=True, exist_ok=True)
    out = s.output_dir / f"verification-{stamp}.xlsx"
    write_excel(tables, out)
    console.print(f"[green]Written:[/green] {out}")

    if recorder is not None:
        try:
            job = mac_check.MacCheckJob(baselines, recorder.path, sheet,
                                        s.output_dir, stamp)
            watch = mac_check.Watching(
                s.cfg.mac_check, targets,
                lambda t: make_runner(t, t.platform, creds, s.cfg, opts),
                s.cfg.ssh.workers) if watching else None
            run_mac_check(job, watch, console)
        except Exception as exc:  # broad on purpose: the verification is already written
            console.print(f"[red]MAC check could not run: {exc}[/red]")


def action_mac_compare(s: Session, console: Console) -> None:
    """Compare two MAC snapshot folders that already exist, without SSH.

    Option `v` takes the after side itself, from the switches. This is the same
    comparison on folders you already have - a baseline against an after side a
    `v` run saved, a baseline against one somebody mailed you, or two baselines
    taken days apart to see what moved before the window even started. It needs
    no collected data and no credentials, so it also works while the session is
    empty. The command-line equivalent is `migration-check diff`.
    """
    raw = _ask(console, "Baseline folder(s) (the BEFORE side), comma-separated",
               default=str(s.mac_snapshot or ""), path=True)
    baselines = [Path(p.strip()) for p in raw.split(",") if p.strip()]
    if not baselines:
        return
    after_raw = _ask(console, "After folder (the AFTER side)", path=True)
    if not after_raw:
        return
    after = Path(after_raw)
    missing = [str(p) for p in [*baselines, after] if not p.is_dir()]
    if missing:
        console.print(f"[red]No such folder: {', '.join(missing)}[/red]")
        return
    for folder in mac_check.unreadable_baselines(baselines):
        console.print(f"[yellow]{folder}: an old VOSS --save-raw capture; its MAC "
                      f"table cannot be read and is left out.[/yellow]")

    # the sheet is optional here: without it a MAC only has to be back in the
    # right VLAN somewhere, with it the verdicts are port-exact
    sheet = None
    sheet_raw = _ask(console, "Cabling sheet for the port map (blank: compare "
                              "without one)", path=True)
    if sheet_raw:
        try:
            sheet = cabling_sheet.load(Path(sheet_raw))
        except cabling_sheet.SheetError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        console.print(f"[dim]Port map: {len(sheet.moved)} moved link(s) from the "
                      f"sheet.[/dim]")
    else:
        console.print("[dim]No port map: a MAC back in the right VLAN on an "
                      "unexpected port cannot be told from one behind an "
                      "uplink.[/dim]")

    s.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    job = mac_check.MacCheckJob(baselines, after, sheet, s.output_dir, stamp)
    try:
        run_mac_check(job, None, console)
    except Exception as exc:  # broad on purpose: a bad folder must not end the session
        console.print(f"[red]Comparison could not run: {exc}[/red]")


def _ask_mac_baselines(s: Session, console: Console) -> tuple[list[Path], bool]:
    """The MAC baseline folder(s) for migration-check, and whether to watch."""
    raw = _ask(console, "MAC baseline folder(s) for the after-check, comma-"
                        "separated (blank: skip)",
               default=str(s.mac_snapshot or ""), path=True)
    baselines = [Path(p.strip()) for p in raw.split(",") if p.strip()]
    missing = [str(p) for p in baselines if not p.is_dir()]
    if missing:
        console.print(f"[yellow]No such folder: {', '.join(missing)} - the MAC "
                      f"check is skipped.[/yellow]")
        return [], False
    for folder in mac_check.unreadable_baselines(baselines):
        console.print(f"[yellow]{folder}: an old VOSS --save-raw capture; its MAC "
                      f"table cannot be read and is left out.[/yellow]")
    watching = bool(baselines) and _yes(
        console, f"Re-read the new switches until every MAC is back? (every "
                 f"{s.cfg.mac_check.interval_seconds:g} s, at most "
                 f"{s.cfg.mac_check.max_rounds} rounds)", False)
    return baselines, watching


def _set_offline(s: Session, console: Console) -> None:
    # asked as a yes/no first, so an offline session can always get back to
    # live SSH - a blank path answer alone would keep the old value
    if not _yes(console, "Replay from a saved capture instead of SSH?",
                default=s.offline_dir is not None):
        s.offline_dir = None
        return
    raw_root = s.output_dir / "raw"
    if raw_root.is_dir():
        found = sorted(p.name for p in raw_root.iterdir() if p.is_dir())[:8]
        console.print(f"[dim]Captures found under {raw_root} "
                      f"({', '.join(found)})[/dim]")
    off = _ask(console, "Offline replay directory",
               str(s.offline_dir) if s.offline_dir
               else (str(raw_root) if raw_root.is_dir() else ""),
               path=True)
    s.offline_dir = Path(off) if off else None


def _set_location_groups(s: Session, console: Console) -> None:
    current = "; ".join(f"{g}={','.join(m)}"
                        for g, m in s.location_groups.items())
    raw = _ask(console, "Groups for this session as NAME=loc1,loc2 "
                        "(semicolon separated, blank = use the config)",
               current)
    try:
        s.location_groups = location_mod.parse_group_args(
            [x for x in raw.split(";") if x.strip()])
    except ValueError as exc:
        console.print(f"[red]{exc}[/red] - keeping the config's groups")
        s.location_groups = {}


def action_settings(s: Session, console: Console) -> None:
    """Pick ONE setting, change it, done - repeat as long as wanted.

    Each choice touches exactly one value, so mistyping (or a crash) can no
    longer cost the answers to every other question on the way through.
    """
    def onoff(v: bool) -> str:
        return "[green]on[/green]" if v else "[dim]off[/dim]"

    while True:
        rows = [
            ("1", "Output directory", str(s.output_dir)),
            ("2", "Offline replay", str(s.offline_dir) if s.offline_dir
             else "[dim]off (live SSH)[/dim]"),
            ("3", "Save raw CLI output (--save-raw)", onoff(s.save_raw)),
            ("4", "Run manifest after collecting", onoff(s.write_manifest)),
            ("5", "Auto-save snapshot after collecting", onoff(s.auto_snapshot)),
            ("6", "Split cabling sheet by location", onoff(s.split_by_location)),
            ("7", "Location groups (this session)",
             "; ".join(f"{g}={','.join(m)}" for g, m in s.location_groups.items())
             or "[dim](from the config)[/dim]"),
            ("8", "NEW switch name", s.new_switch or "[dim](unset)[/dim]"),
        ]
        t = RichTable(title="Settings", title_justify="left", show_header=False,
                      box=None, pad_edge=False)
        t.add_column(justify="right", style="bold cyan", width=3)
        t.add_column(style="bold")
        t.add_column()
        for row in rows:
            t.add_row(*row)
        console.print(t)
        pick = _ask(console, "Change which setting? (blank = back)")
        if not pick:
            return
        if _change_setting(s, console, pick):
            console.print("[green]Setting updated.[/green]")
        else:
            console.print(f"[red]Unknown setting '{pick}'.[/red]")


def _change_setting(s: Session, console: Console, pick: str) -> bool:
    """Change the one setting picked; False when there is no such setting."""
    if pick == "1":
        s.output_dir = Path(_ask(console, "Output directory",
                                 str(s.output_dir), path=True))
    elif pick == "2":
        _set_offline(s, console)
    elif pick == "3":
        s.save_raw = _yes(console, "Save raw CLI output (--save-raw)?",
                          s.save_raw)
    elif pick == "4":
        s.write_manifest = _yes(
            console, "Write a run manifest after collecting?",
            s.write_manifest)
    elif pick == "5":
        s.auto_snapshot = _yes(
            console, "Auto-save a snapshot after every collection?",
            s.auto_snapshot)
    elif pick == "6":
        s.split_by_location = _yes(
            console,
            "Split the cabling sheet into one worksheet per location?",
            s.split_by_location)
        if s.split_by_location:
            _set_location_groups(s, console)
    elif pick == "7":
        _set_location_groups(s, console)
    elif pick == "8":
        s.new_switch = _ask(console, "Name of the NEW switch", s.new_switch)
    else:
        return False
    return True


def _apply_profile(s: Session, name: str, prof: dict[str, Any], console: Console) -> None:
    """Set the session from a profile. Only SETS values - everything stays
    changeable on the fly afterwards (settings, selection, everything)."""
    applied: list[str] = []
    if "inventory" in prof:
        try:
            s.all_targets = load_inventory(prof["inventory"])
            s.inventory_path = prof["inventory"]
            s.selected = list(s.all_targets)
            applied.append(f"inventory {prof['inventory']} "
                           f"({len(s.all_targets)} switches)")
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red] - inventory not changed")
    if "output_dir" in prof:
        s.output_dir = prof["output_dir"]
        applied.append(f"output {s.output_dir}")
    if "offline" in prof:
        s.offline_dir = prof["offline"]
        applied.append(f"offline replay {s.offline_dir}")
    for key, attr in (("save_raw", "save_raw"), ("manifest", "write_manifest"),
                      ("auto_snapshot", "auto_snapshot"),
                      ("split_by_location", "split_by_location")):
        if key in prof:
            setattr(s, attr, prof[key])
            applied.append(f"{key} {'on' if prof[key] else 'off'}")
    if "location_groups" in prof:
        s.location_groups = dict(prof["location_groups"])
        applied.append(f"{len(s.location_groups)} location group(s)")
    if "new_switch" in prof:
        s.new_switch = prof["new_switch"]
        applied.append(f"new switch '{s.new_switch}'")
    console.print(f"[green]Profile '{name}' loaded:[/green] "
                  + ("; ".join(applied) if applied else "(empty profile)"))
    console.print("[dim]Everything can still be changed on the fly "
                  "(options 1 and s).[/dim]")


def _session_profile(s: Session) -> dict[str, Any]:
    """The session's current shape, as a profile dict worth saving."""
    prof: dict[str, Any] = {
        "output_dir": s.output_dir,
        "save_raw": s.save_raw,
        "manifest": s.write_manifest,
        "auto_snapshot": s.auto_snapshot,
        "split_by_location": s.split_by_location,
    }
    if s.inventory_path:
        prof["inventory"] = s.inventory_path
    if s.offline_dir:
        prof["offline"] = s.offline_dir
    if s.location_groups:
        prof["location_groups"] = dict(s.location_groups)
    if s.new_switch:
        prof["new_switch"] = s.new_switch
    return prof


def action_handover(s: Session, console: Console) -> None:
    """Everything this session produced, in one folder with an index page.

    Reuses whatever is already in the output directory rather than re-running
    anything: the bundle is a way to hand work over, not a way to do it again.
    """
    tables = build_report(s.audits, s.fabric or FabricState(), s.cfg,
                          ReportOptions(fabric_checked=s.collected_fabric)).tables

    files = sorted(p for p in s.output_dir.glob("*")
                   if p.is_file() and not p.name.startswith("switch-migrator"))
    if not files:
        console.print("[yellow]Nothing in the output directory yet - produce a "
                      "report first (options 3-7).[/yellow]")
        return
    bundle = handover_mod.build(s.output_dir, handover_mod.Contents(
        files, tables, s.audits,
        meta={"switches": ", ".join(a.name for a in s.audits),
              "config": str(s.config_path) if s.config_path else ""}))
    console.print(f"[green]Handover bundle:[/green] {bundle}")
    console.print(f"  open [bold]{bundle / 'index.html'}[/bold]")


def action_profiles(s: Session, console: Console) -> None:
    """Named scenarios: 'scenario A always needs inventory X and settings Y'
    becomes one load. Profiles never hold credentials."""
    try:
        profiles = profiles_mod.load_profiles(s.profiles_path)
    except profiles_mod.ProfileError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    names = sorted(profiles)
    if names:
        t = RichTable(title=f"Profiles ({s.profiles_path})",
                      title_justify="left")
        t.add_column("#", justify="right")
        t.add_column("Name")
        t.add_column("Inventory")
        t.add_column("Output")
        t.add_column("New switch")
        for i, name in enumerate(names, 1):
            p = profiles[name]
            t.add_row(str(i), name, str(p.get("inventory", "")),
                      str(p.get("output_dir", "")), p.get("new_switch", ""))
        console.print(t)
    else:
        console.print(f"[dim]No profiles in {s.profiles_path} yet.[/dim]")
    console.print("[bold]1[/bold] load a profile   "
                  "[bold]2[/bold] save the current session as a profile")
    choice = _ask(console, "Choose", "1" if names else "2")
    if choice == "1":
        _load_profile(s, profiles, console)
    elif choice == "2":
        _save_profile(s, console)


def _load_profile(s: Session, profiles: dict[str, dict[str, Any]],
                  console: Console) -> None:
    names = sorted(profiles)
    if not names:
        console.print("[yellow]Nothing to load - save one first.[/yellow]")
        return
    raw = _ask(console, "Profile (number or name)")
    if not raw:
        return
    if raw.isdigit() and 1 <= int(raw) <= len(names):
        name = names[int(raw) - 1]
    elif raw in profiles:
        name = raw
    else:
        console.print(f"[red]No profile '{raw}'.[/red]")
        return
    _apply_profile(s, name, profiles[name], console)


def _save_profile(s: Session, console: Console) -> None:
    name = _ask(console, "Save as profile name")
    if not name:
        return
    try:
        written = profiles_mod.save_profile(name, _session_profile(s),
                                            s.profiles_path)
    except (OSError, profiles_mod.ProfileError) as exc:
        console.print(f"[red]could not save the profile: {exc}[/red]")
        return
    console.print(f"[green]Profile '{name}' saved to {written}.[/green]")


# --------------------------------------------------------------------------- #
# loop
# --------------------------------------------------------------------------- #

def _default_creds(s: Session, console: Console
                   ) -> tuple[Credentials | None, Credentials | None]:
    """Resolve credentials the same way the flag interface does."""

    if s.offline_dir:
        offline = Credentials("offline", "offline")
        return offline, offline
    try:
        creds = get_credentials("switches", "SM")
        dvr_creds = (get_credentials("DvR controllers", "SM_DVR", fallback=creds)
                     if s.cfg.dvr_controllers else creds)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return None, None
    if s.cfg.ssh.legacy_algorithms:
        enable_legacy_ssh_algorithms()
    return creds, dvr_creds


@dataclass(frozen=True)
class MenuStart:
    """What the menu opens with: the CLI's -c/-i/-o/--profile/--profiles-file."""
    config_path: Path
    inventory_path: Path | None = None
    output_dir: Path = Path("output")
    profile: str = ""
    profiles_file: Path | None = None


def run_menu(start: MenuStart, console: Console | None = None,
             creds_fn: CredsFn = _default_creds) -> int:
    """The interactive toolkit menu. Returns a process exit code."""
    console = console or Console()
    try:
        # the menu is exploratory: a missing fabric section must not block it
        cfg = load_config(start.config_path, require_fabric=False)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 2
    s = _open_session(start, cfg, console)
    if start.profile:
        _start_profile(s, start, console)
    return _menu_loop(s, console, _menu_actions(s, console, creds_fn))


def _open_session(start: MenuStart, cfg: Config, console: Console) -> Session:
    inventory_path = start.inventory_path
    if inventory_path is None:
        # default inventory: the config's `inventory:` key, or a
        # ./switches.yaml - so the everyday session starts ready to collect
        inventory_path = default_inventory(cfg)
        if inventory_path is not None:
            console.print(f"[dim]Using default inventory: {inventory_path}[/dim]")
    s = Session(config_path=start.config_path, cfg=cfg, inventory_path=inventory_path,
                output_dir=start.output_dir)
    if inventory_path:
        try:
            s.all_targets = load_inventory(inventory_path)
            s.selected = list(s.all_targets)
        except ConfigError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
    return s


def _start_profile(s: Session, start: MenuStart, console: Console) -> None:
    """The session carries settings the CLI namespace has no place for, so
    the profile is applied here in full - and a bad name is said out loud
    rather than silently leaving the menu on its defaults."""
    if start.profiles_file is not None:
        s.profiles_path = start.profiles_file
    try:
        found = profiles_mod.load_profiles(s.profiles_path).get(start.profile)
    except profiles_mod.ProfileError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        found = None
    if found is None:
        console.print(f"[yellow]No profile '{start.profile}' in "
                      f"{s.profiles_path} - starting on the defaults.[/yellow]")
    else:
        _apply_profile(s, start.profile, found, console)


def _menu_actions(s: Session, console: Console,
                  creds_fn: CredsFn) -> dict[str, Callable[[], None]]:
    """One action per menu key."""
    return {
        "1": lambda: action_select(s, console),
        "2": lambda: action_collect(s, console, creds_fn),
        "3": lambda: action_audit(s, console),
        "4": lambda: action_inventory(s, console),
        "5": lambda: action_sheets(s, console),
        "6": lambda: action_config(s, console),
        "7": lambda: action_everything(s, console),
        "8": lambda: action_snapshot(s, console),
        "9": lambda: action_dry_run(s, console),
        "h": lambda: action_health(s, console),
        "m": lambda: action_generate_mlt(s, console),
        "r": lambda: action_rack_sheet(s, console),
        "v": lambda: action_verify(s, console, creds_fn),
        "c": lambda: action_mac_compare(s, console),
        "b": lambda: action_handover(s, console),
        "p": lambda: action_profiles(s, console),
        "s": lambda: action_settings(s, console),
    }


def _menu_loop(s: Session, console: Console,
               actions: dict[str, Callable[[], None]]) -> int:
    idle_rounds = 0
    while True:
        console.print()
        console.print(_status_panel(s))
        console.print(_menu_table(s))
        console.print("[dim]'x' at any prompt aborts the current action; "
                      "0/q quits.[/dim]")
        try:
            choice = _ask(console, "Choose").lower()
        except Abort as abort:
            if abort.interrupt:
                # Ctrl-C at the menu itself: nothing to abandon, so it quits.
                # Continuing here left no way out of the loop at all.
                console.print("[dim]Bye.[/dim]")
                return 0
            continue
        if choice in ("0", "q", "quit", "exit"):
            console.print("[dim]Bye.[/dim]")
            return 0
        if not choice:
            # a stray Enter re-shows the menu instead of quitting - quitting
            # is an explicit 0/q. A closed stdin (blank forever) still exits.
            idle_rounds += 1
            if idle_rounds >= MAX_IDLE_ROUNDS:
                console.print("[dim]No input - bye.[/dim]")
                return 0
            continue
        idle_rounds = 0
        action = actions.get(choice)
        if action is None:
            console.print(f"[red]Unknown choice '{choice}'.[/red]")
            continue
        if choice in _NEEDS_DATA and not s.has_data:
            console.print("[yellow]Collect from the devices first (option 2).[/yellow]")
            continue
        try:
            action()
        except Abort:
            console.print("\n[yellow]Aborted - back to the menu.[/yellow]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
        except Exception as exc:  # broad on purpose: never kill the menu on one action
            console.print(f"[red]{exc.__class__.__name__}: {exc}[/red]")
