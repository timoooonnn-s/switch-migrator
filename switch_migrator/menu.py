"""Interactive menu - the toolkit front end.

Running `switch-migrator` with no arguments drops you here. The menu owns a
SESSION: devices are collected ONCE and every output (audit report, inventory
report, migration sheets, config extract) is produced from that same data, so
switching between use cases costs no further SSH round-trips.

All command-line flags keep working unchanged; this is an additional entry
point, not a replacement.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable

from switch_migrator.config import (
    Config,
    ConfigError,
    Credentials,
    SwitchTarget,
    default_inventory,
    load_config,
    load_inventory,
    parse_switch_arg,
)
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator import handover as handover_mod
from switch_migrator import health as health_mod
from switch_migrator import location as location_mod
from switch_migrator import manifest as manifest_mod
from switch_migrator import profiles as profiles_mod
from switch_migrator import snapshot as snapshot_mod
from switch_migrator.report.progress import make_progress


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
    location_groups: dict = field(default_factory=dict)
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
    commands_by_device: dict = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.audits)

    def location_rules(self):
        """Config rules, with this session's per-run grouping when set."""
        from dataclasses import replace as _replace
        if not self.location_groups:
            return self.cfg.locations
        return _replace(self.cfg.locations, groups=self.location_groups)

    def to_args(self, **over) -> argparse.Namespace:
        """A Namespace shaped like the CLI's, so the menu can reuse the exact
        same collection/report code paths as the flag interface."""
        base = dict(
            config=self.config_path, inventory=self.inventory_path, switch=[],
            output_dir=self.output_dir, no_fabric=not self.collected_fabric,
            extract_config=False, migration_sheets=False,
            new_switch=self.new_switch, csv=False, no_excel=False,
            save_raw=self.save_raw, offline=self.offline_dir, verbose=False,
            debug=False, dry_run=False, save_snapshot=None, from_snapshot=None,
            manifest=self.write_manifest,
            split_by_location=self.split_by_location, location_group=[],
        )
        base.update(over)
        return argparse.Namespace(**base)


class Abort(Exception):
    """The user typed 'x' (or pressed Ctrl-C) at a prompt: abandon the current
    action immediately and fall back to the main menu. Every prompt honours
    it, so any process can be aborted at any state."""


@contextmanager
def _path_completion():
    """Readline tab-completion for filesystem paths, active only around a
    path prompt. Degrades to nothing where readline is unavailable."""
    try:
        import glob
        import readline
    except ImportError:                     # e.g. Windows without pyreadline3
        yield
        return

    def complete(text: str, state: int):
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
        raise Abort() from None
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
        lines.append(f"[bold]Mode:[/bold]      [yellow]OFFLINE replay from {s.offline_dir}[/yellow]")
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
    ("v", "Verify migration", "after the window: check every re-patched link"),
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
    if s.inventory_path and not s.all_targets:
        try:
            s.all_targets = load_inventory(s.inventory_path)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
    if not s.all_targets:
        path = _ask(console, "Inventory YAML path (blank to add switches by hand)",
                    path=True)
        if path:
            try:
                s.inventory_path = Path(path)
                s.all_targets = load_inventory(s.inventory_path)
            except ConfigError as exc:
                console.print(f"[red]{exc}[/red]")
                return
        else:
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
        picked: list[SwitchTarget] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                if "-" in chunk:
                    a, b = (int(x) for x in chunk.split("-", 1))
                    picked += s.all_targets[a - 1:b]
                else:
                    picked.append(s.all_targets[int(chunk) - 1])
            except (ValueError, IndexError):
                console.print(f"[red]ignoring '{chunk}'[/red]")
        if picked:
            s.selected = picked
    console.print(f"[green]{len(s.selected)} switch(es) selected.[/green]")


def _preflight_credentials(s: Session, console: Console, creds):
    """One quick SSH login against one device BEFORE the full run, so wrong
    credentials fail in seconds instead of a read_timeout per device - and can
    be re-entered on the spot. Returns the (possibly corrected) credentials,
    or None to abort the collection."""
    from switch_migrator.connection import quick_auth_check

    if s.offline_dir is not None:
        return creds                    # replay: nothing to authenticate
    # console-routed switches authenticate differently; probe a plain one
    probe = next((t for t in s.selected if not getattr(t, "console", "")), None)
    if probe is None:
        return creds
    for _ in range(3):
        console.print(f"[dim]Checking credentials against {probe.name} "
                      f"({probe.host})...[/dim]")
        result = quick_auth_check(probe.host, creds, s.cfg.ssh)
        if result is None:
            console.print("[green]Credentials OK.[/green]")
            return creds
        if result != "auth":
            # unreachable / algorithm trouble is not proof the creds are wrong
            console.print(f"[yellow]Could not verify credentials against "
                          f"{probe.name}: {result}[/yellow]")
            if _yes(console, "Continue with the collection anyway?"):
                return creds
            return None
        console.print(f"[red]{probe.name} refused the credentials.[/red]")
        user = _ask(console, "Username", creds.username)
        try:
            pw = console.input("[bold cyan]Password:[/bold cyan] ",
                               password=True)
        except (EOFError, KeyboardInterrupt):
            return None
        if not user or not pw:
            return None
        creds = Credentials(user, pw)
    console.print("[red]Still refused after 3 attempts - aborting the "
                  "collection (no lockout risk taken).[/red]")
    return None


def action_collect(s: Session, console: Console, creds_fn) -> None:
    from switch_migrator.cli import collect_fabric, run_collection

    if not s.selected:
        console.print("[yellow]No switches selected - use option 1 first.[/yellow]")
        return
    want_fabric = bool(s.cfg.dvr_controllers) and _yes(
        console, f"Collect fabric state from {len(s.cfg.dvr_controllers)} DvR "
                 f"controller(s)? (needed for the audit comparison)")
    want_config = _yes(console, "Also pull running-config? (needed for config "
                                "extract; also gives the sheets tagged/untagged)")
    want_macs = _yes(console, "Also collect MAC tables? (needed for migration sheets)")

    creds, dvr_creds = creds_fn(s, console)
    if creds is None:
        return
    checked = _preflight_credentials(s, console, creds)
    if checked is None:
        return
    if dvr_creds is creds:
        # the DvR sessions were reusing the switch credentials - keep them in
        # step when the pre-flight corrected those
        dvr_creds = checked
    creds = checked

    args = s.to_args(no_fabric=not want_fabric, extract_config=want_config,
                     migration_sheets=want_macs)
    started = datetime.now()
    commands: dict = {}
    fabric = FabricState()
    # the fabric read and the switch reads are independent - overlap them so
    # the DvR round-trip is not in front of every device
    with make_progress(console, len(s.selected),
                       len(s.cfg.dvr_controllers) if want_fabric else 0) as prog:
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            future = (pool.submit(collect_fabric, s.cfg.dvr_controllers,
                                  dvr_creds, s.cfg, args, prog)
                      if want_fabric else None)
            audits = run_collection(s.selected, creds, s.cfg, args, prog, commands)
            if future is not None:
                fabric = future.result()
    if want_fabric:
        if not fabric.dvrs_ok:
            console.print("[red]No DvR controller could be read - continuing "
                          "without fabric data (audit comparison unavailable).[/red]")
            want_fabric = False
        else:
            console.print(f"[green]Fabric: {len(fabric.isids)} I-SIDs from "
                          f"{', '.join(fabric.dvrs_ok)}[/green]")

    s.audits, s.fabric = audits, fabric
    s.collected_at = datetime.now()
    s.collected_fabric = want_fabric
    # The migration-sheet collection reads the running-config too, for the
    # tagging - but drops the text again unless it was asked for. So what is
    # RETAINED, which is what the config extract and the snapshot need, is
    # still exactly what the user said yes to here.
    s.collected_config = want_config
    s.collected_macs = want_macs
    s.loaded_from = None
    s.commands_by_device = commands
    ok = sum(1 for a in audits if a.reachable)
    console.print(f"[green]Collected {ok}/{len(audits)} switch(es).[/green]")
    for a in audits:
        if not a.reachable:
            console.print(f"  [red]UNREACHABLE {a.name}: "
                          f"{a.errors[-1] if a.errors else 'unknown'}[/red]")
    if s.auto_snapshot and s.audits:
        # crash insurance: the expensive part is now on disk, timestamped;
        # option 8 lists these for one-keystroke reload
        path = (s.output_dir / "snapshots"
                / f"snapshot-{started.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            snapshot_mod.save(path, s.audits, s.fabric or FabricState(),
                              meta=_snapshot_meta(s))
            console.print(f"[dim]Auto-saved snapshot: {path}[/dim]")
        except OSError as exc:
            console.print(f"[yellow]could not auto-save a snapshot: {exc}[/yellow]")
    if s.write_manifest:
        s.output_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_mod.write(
            s.output_dir / f"manifest-{started.strftime('%Y%m%d-%H%M%S')}.json",
            manifest_mod.build(audits, fabric, args, started, datetime.now(),
                               [], commands, config_path=s.config_path))
        console.print(f"[green]Run manifest:[/green] {path}")


def _write_outputs(s: Session, console: Console, *, no_fabric: bool,
                   extract_config: bool, migration_sheets: bool,
                   title: str) -> None:
    """Build the tables for one use case and write the report files."""
    from switch_migrator.cli import _write_config_extracts
    from switch_migrator.compare import compare_switch, resolve_binding_isids
    from switch_migrator.report import console as console_report
    from switch_migrator.report.excel import write_excel
    from switch_migrator.report.migration import (
        assign_port_uids, build_cabling, build_cabling_by_location,
        build_commands, build_port_info, new_switch_names)
    from switch_migrator.report.tables import build_all

    fabric = s.fabric or FabricState()
    comparisons = {} if no_fabric else {
        a.name: compare_switch(a, fabric, s.cfg) for a in s.audits if a.reachable}
    resolve_binding_isids(s.audits, comparisons,
                          fabric_checked=not no_fabric)
    tables = build_all(s.audits, fabric, comparisons, no_fabric=no_fabric)
    if migration_sheets:
        assign_port_uids(s.audits)
        tables.append(build_port_info(s.audits))
        if s.split_by_location:
            rules = s.location_rules()
            tables += build_cabling_by_location(
                s.audits, rules, new_switch_names(s.new_switch))
            console.print("[dim]Cabling split by location:[/dim]")
            for line in location_mod.describe(rules, [a.name for a in s.audits]):
                console.print(f"  [dim]{line}[/dim]")
        else:
            tables.append(
                build_cabling(s.audits, new_switch_names(s.new_switch)))

    console_report.render(tables, console, verbose=False)

    s.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written = [s.output_dir / f"{title}-{stamp}.xlsx"]
    write_excel(tables, written[0])
    if migration_sheets:
        cmd = s.output_dir / f"migration-commands-{stamp}.txt"
        cmd.write_text(build_commands(s.audits, new_switch=s.new_switch),
                       encoding="utf-8")
        written.append(cmd)
    if extract_config:
        written += _write_config_extracts(s.audits, s.output_dir, comparisons,
                                          s.cfg, console)
    for path in written:
        console.print(f"[green]Written:[/green] {path}")


def action_audit(s: Session, console: Console) -> None:
    if not s.collected_fabric:
        console.print("[yellow]No fabric data in this session - the audit needs "
                      "it. Re-collect (option 2) and answer yes to the fabric "
                      "question, or use the inventory report (option 4).[/yellow]")
        return
    _write_outputs(s, console, no_fabric=False, extract_config=False,
                   migration_sheets=False, title="migration-audit")


def action_inventory(s: Session, console: Console) -> None:
    _write_outputs(s, console, no_fabric=True, extract_config=False,
                   migration_sheets=False, title="inventory-report")


def action_sheets(s: Session, console: Console) -> None:
    if not s.collected_macs:
        console.print("[yellow]No MAC data in this session - the sheets will "
                      "have empty MAC columns. Re-collect (option 2) and answer "
                      "yes to the MAC question for full sheets.[/yellow]")
        if not _yes(console, "Continue anyway?", default=False):
            return
    if not s.new_switch:
        s.new_switch = _ask(console, "Name of the NEW switch (for the commands file)")
    _write_outputs(s, console, no_fabric=not s.collected_fabric,
                   extract_config=False, migration_sheets=True,
                   title="migration-sheets")


def action_config(s: Session, console: Console) -> None:
    if not s.collected_config:
        console.print("[yellow]No running-config in this session. Re-collect "
                      "(option 2) and answer yes to the running-config "
                      "question.[/yellow]")
        return
    _write_outputs(s, console, no_fabric=not s.collected_fabric,
                   extract_config=True, migration_sheets=False,
                   title="config-extract")


def action_everything(s: Session, console: Console) -> None:
    if not s.new_switch:
        s.new_switch = _ask(console, "Name of the NEW switch (for the commands file)")
    _write_outputs(s, console, no_fabric=not s.collected_fabric,
                   extract_config=s.collected_config, migration_sheets=True,
                   title="migration-full")


def _snapshot_meta(s: Session) -> dict:
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
    from switch_migrator.cli import preview_commands, render_dry_run

    if not s.selected:
        console.print("[yellow]No switches selected - use option 1 first.[/yellow]")
        return
    want_config = _yes(console, "Include the running-config pull?", False)
    want_macs = _yes(console, "Include the MAC/optics collection?", True)
    args = s.to_args(extract_config=want_config, migration_sheets=want_macs)
    render_dry_run(preview_commands(s.selected, s.cfg, args,
                                    dvrs=s.cfg.dvr_controllers), console)


def action_health(s: Session, console: Console) -> None:
    """Go/no-go on the collected data. Sends nothing to any device."""
    from switch_migrator.compare import compare_switch
    from switch_migrator.report import console as console_report
    from switch_migrator.report.excel import write_excel
    from switch_migrator.report.migration_tables import (
        build_health, build_health_summary)

    fabric = s.fabric or FabricState()
    comparisons = {} if not s.collected_fabric else {
        a.name: compare_switch(a, fabric, s.cfg) for a in s.audits if a.reachable}
    report = health_mod.check(s.audits, comparisons, s.cfg)
    tables = [build_health_summary(report), build_health(report)]
    console_report.render(tables, console, verbose=False)

    counts = report.counts()
    color = {health_mod.BLOCK: "bold red", health_mod.WARN: "bold yellow",
             health_mod.OK: "bold green"}.get(report.verdict, "bold")
    console.print(f"[{color}]Pre-migration health: {report.verdict}[/{color}] - "
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
    from switch_migrator import cabling_sheet, mlt_generate

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


def action_verify(s: Session, console: Console, creds_fn) -> None:
    """Read the filled-in sheet, collect the NEW switches, check every link."""
    from switch_migrator import cabling_sheet, verify
    from switch_migrator.cli import run_collection
    from switch_migrator.report import console as console_report
    from switch_migrator.report.excel import write_excel
    from switch_migrator.report.migration_tables import (
        build_verification, build_verification_summary)

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

    creds, _ = creds_fn(s, console)
    if creds is None:
        return
    # the MACs on the new ports are the evidence, so collect them
    args = s.to_args(no_fabric=True, migration_sheets=True)
    with make_progress(console, len(targets)) as prog:
        audits = run_collection(targets, creds, s.cfg, args, prog, {})

    report = verify.verify(sheet, audits)
    extra = verify.unexpected_ports(sheet, audits)
    tables = [build_verification_summary(report, extra),
              build_verification(report)]
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
    out = s.output_dir / f"verification-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    write_excel(tables, out)
    console.print(f"[green]Written:[/green] {out}")


def _set_offline(s: Session, console: Console) -> None:
    # asked as a yes/no first, so an offline session can always get back to
    # live SSH (a blank path answer used to just keep the old value)
    if not _yes(console, "Replay from a saved capture instead of SSH?",
                default=s.offline_dir is not None):
        s.offline_dir = None
        return
    raw_root = s.output_dir / "raw"
    if raw_root.is_dir():
        console.print(f"[dim]Captures found under {raw_root} "
                      f"({', '.join(sorted(p.name for p in raw_root.iterdir() if p.is_dir())[:8])})[/dim]")
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
            console.print(f"[red]Unknown setting '{pick}'.[/red]")
            continue
        console.print("[green]Setting updated.[/green]")


def _apply_profile(s: Session, name: str, prof: dict, console: Console) -> None:
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


def _session_profile(s: Session) -> dict:
    """The session's current shape, as a profile dict worth saving."""
    prof: dict = {
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
    from switch_migrator.compare import compare_switch, resolve_binding_isids
    from switch_migrator.report.tables import build_all

    fabric = s.fabric or FabricState()
    comparisons = {} if not s.collected_fabric else {
        a.name: compare_switch(a, fabric, s.cfg) for a in s.audits if a.reachable}
    resolve_binding_isids(s.audits, comparisons,
                          fabric_checked=s.collected_fabric)
    tables = build_all(s.audits, fabric, comparisons,
                       no_fabric=not s.collected_fabric)

    files = sorted(p for p in s.output_dir.glob("*")
                   if p.is_file() and not p.name.startswith("switch-migrator"))
    if not files:
        console.print("[yellow]Nothing in the output directory yet - produce a "
                      "report first (options 3-7).[/yellow]")
        return
    bundle = handover_mod.build(
        s.output_dir, files, tables, s.audits,
        meta={"switches": ", ".join(a.name for a in s.audits),
              "config": str(s.config_path) if s.config_path else ""})
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
    elif choice == "2":
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

def _default_creds(s: Session, console: Console):
    """Resolve credentials the same way the flag interface does."""
    from switch_migrator.config import get_credentials
    from switch_migrator.connection import enable_legacy_ssh_algorithms

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


def run_menu(config_path: Path, inventory_path: Path | None = None,
             output_dir: Path = Path("output"),
             console: Console | None = None,
             creds_fn=_default_creds) -> int:
    """The interactive toolkit menu. Returns a process exit code."""
    console = console or Console()
    try:
        # the menu is exploratory: a missing fabric section must not block it
        cfg = load_config(config_path, require_fabric=False)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 2

    if inventory_path is None:
        # default inventory: the config's `inventory:` key, or a
        # ./switches.yaml - so the everyday session starts ready to collect
        inventory_path = default_inventory(cfg)
        if inventory_path is not None:
            console.print(f"[dim]Using default inventory: {inventory_path}[/dim]")
    s = Session(config_path=config_path, cfg=cfg, inventory_path=inventory_path,
                output_dir=output_dir)
    if inventory_path:
        try:
            s.all_targets = load_inventory(inventory_path)
            s.selected = list(s.all_targets)
        except ConfigError as exc:
            console.print(f"[yellow]{exc}[/yellow]")

    actions = {
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
        "v": lambda: action_verify(s, console, creds_fn),
        "b": lambda: action_handover(s, console),
        "p": lambda: action_profiles(s, console),
        "s": lambda: action_settings(s, console),
    }
    idle_rounds = 0
    while True:
        console.print()
        console.print(_status_panel(s))
        console.print(_menu_table(s))
        console.print("[dim]'x' at any prompt aborts the current action; "
                      "0/q quits.[/dim]")
        try:
            choice = _ask(console, "Choose").lower()
        except Abort:
            continue
        if choice in ("0", "q", "quit", "exit"):
            console.print("[dim]Bye.[/dim]")
            return 0
        if not choice:
            # a stray Enter re-shows the menu instead of quitting - quitting
            # is an explicit 0/q. A closed stdin (blank forever) still exits.
            idle_rounds += 1
            if idle_rounds >= 50:
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
        except Exception as exc:  # noqa: BLE001 - never kill the menu on one action
            console.print(f"[red]{exc.__class__.__name__}: {exc}[/red]")
