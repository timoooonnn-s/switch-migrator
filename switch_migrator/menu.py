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
    load_config,
    load_inventory,
    parse_switch_arg,
)
from switch_migrator.models import FabricState, Platform, SwitchAudit


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
    new_switch: str = ""
    # collected data
    audits: list[SwitchAudit] = field(default_factory=list)
    fabric: FabricState | None = None
    collected_at: datetime | None = None
    collected_fabric: bool = False
    collected_config: bool = False
    collected_macs: bool = False

    @property
    def has_data(self) -> bool:
        return bool(self.audits)

    def to_args(self, **over) -> argparse.Namespace:
        """A Namespace shaped like the CLI's, so the menu can reuse the exact
        same collection/report code paths as the flag interface."""
        base = dict(
            config=self.config_path, inventory=self.inventory_path, switch=[],
            output_dir=self.output_dir, no_fabric=not self.collected_fabric,
            extract_config=False, migration_sheets=False,
            new_switch=self.new_switch, csv=False, no_excel=False,
            save_raw=self.save_raw, offline=self.offline_dir, verbose=False,
            debug=False,
        )
        base.update(over)
        return argparse.Namespace(**base)


def _ask(console: Console, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = console.input(f"[bold cyan]{prompt}{suffix}:[/bold cyan] ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
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
        lines.append(f"[bold green]Data:[/bold green]      collected {when} "
                     f"({len(s.audits)} switch(es)"
                     + (f", incl. {', '.join(extras)}" if extras else "") + ")")
    else:
        lines.append("[bold yellow]Data:[/bold yellow]      not collected yet")
    return Panel("\n".join(lines), title="switch-migrator", title_align="left")


_ENTRIES = [
    ("1", "Select switches", "pick targets from the inventory or add them by hand"),
    ("2", "Collect from devices", "connect once; all outputs below reuse this data"),
    ("3", "Audit vs fabric", "compare every VLAN against the DvR fabric state"),
    ("4", "Inventory report", "port/MLT/VLAN state only, no fabric comparison"),
    ("5", "Migration sheets", "port info + DC cabling sheet + MAC-check commands"),
    ("6", "Config extract", "neutralized VOSS config / generated ERS->VOSS draft"),
    ("7", "Everything", "run 3-6 in one go with the collected data"),
    ("8", "Settings", "output directory, offline replay, target switch name"),
    ("0", "Quit", ""),
]


def _menu_table(s: Session) -> RichTable:
    t = RichTable(show_header=False, box=None, pad_edge=False)
    t.add_column(justify="right", style="bold cyan", width=3)
    t.add_column(style="bold")
    t.add_column(style="dim")
    for key, label, hint in _ENTRIES:
        needs_data = key in ("3", "4", "5", "6", "7") and not s.has_data
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
        path = _ask(console, "Inventory YAML path (blank to add switches by hand)")
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


def action_collect(s: Session, console: Console, creds_fn) -> None:
    from switch_migrator.cli import collect_fabric, run_collection

    if not s.selected:
        console.print("[yellow]No switches selected - use option 1 first.[/yellow]")
        return
    want_fabric = bool(s.cfg.dvr_controllers) and _yes(
        console, f"Collect fabric state from {len(s.cfg.dvr_controllers)} DvR "
                 f"controller(s)? (needed for the audit comparison)")
    want_config = _yes(console, "Also pull running-config? (needed for config extract)")
    want_macs = _yes(console, "Also collect MAC tables? (needed for migration sheets)")

    creds, dvr_creds = creds_fn(s, console)
    if creds is None:
        return

    args = s.to_args(no_fabric=not want_fabric, extract_config=want_config,
                     migration_sheets=want_macs)
    fabric = FabricState()
    if want_fabric:
        with console.status("Collecting fabric state from DvR controllers..."):
            fabric = collect_fabric(s.cfg.dvr_controllers, dvr_creds, s.cfg, args)
        if not fabric.dvrs_ok:
            console.print("[red]No DvR controller could be read - continuing "
                          "without fabric data (audit comparison unavailable).[/red]")
            want_fabric = False
        else:
            console.print(f"[green]Fabric: {len(fabric.isids)} I-SIDs from "
                          f"{', '.join(fabric.dvrs_ok)}[/green]")

    with console.status(f"Collecting from {len(s.selected)} switch(es)..."):
        audits = run_collection(s.selected, creds, s.cfg, args)

    s.audits, s.fabric = audits, fabric
    s.collected_at = datetime.now()
    s.collected_fabric = want_fabric
    s.collected_config = want_config
    s.collected_macs = want_macs
    ok = sum(1 for a in audits if a.reachable)
    console.print(f"[green]Collected {ok}/{len(audits)} switch(es).[/green]")
    for a in audits:
        if not a.reachable:
            console.print(f"  [red]UNREACHABLE {a.name}: "
                          f"{a.errors[-1] if a.errors else 'unknown'}[/red]")


def _write_outputs(s: Session, console: Console, *, no_fabric: bool,
                   extract_config: bool, migration_sheets: bool,
                   title: str) -> None:
    """Build the tables for one use case and write the report files."""
    from switch_migrator.cli import _write_config_extracts
    from switch_migrator.compare import compare_switch
    from switch_migrator.report import console as console_report
    from switch_migrator.report.excel import write_excel
    from switch_migrator.report.migration import (
        assign_port_uids, build_cabling, build_commands, build_port_info)
    from switch_migrator.report.tables import build_all

    fabric = s.fabric or FabricState()
    comparisons = {} if no_fabric else {
        a.name: compare_switch(a, fabric, s.cfg) for a in s.audits if a.reachable}
    tables = build_all(s.audits, fabric, comparisons, no_fabric=no_fabric)
    if migration_sheets:
        assign_port_uids(s.audits)
        tables += [build_port_info(s.audits), build_cabling(s.audits)]

    console_report.render(tables, console, verbose=False)

    s.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written = [s.output_dir / f"{title}-{stamp}.xlsx"]
    write_excel(tables, written[0])
    if migration_sheets:
        cmd = s.output_dir / f"migration-commands-{stamp}.txt"
        cmd.write_text(build_commands(s.audits, new_switch=s.new_switch))
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


def action_settings(s: Session, console: Console) -> None:
    out = _ask(console, "Output directory", str(s.output_dir))
    s.output_dir = Path(out)
    # asked as a yes/no first, so an offline session can always get back to
    # live SSH (a blank path answer used to just keep the old value)
    if _yes(console, "Replay from a saved capture instead of SSH?",
            default=s.offline_dir is not None):
        off = _ask(console, "Offline replay directory",
                   str(s.offline_dir) if s.offline_dir else "")
        s.offline_dir = Path(off) if off else None
    else:
        s.offline_dir = None
    s.save_raw = _yes(console, "Save raw CLI output (--save-raw)?", s.save_raw)
    s.new_switch = _ask(console, "Name of the NEW switch", s.new_switch)
    console.print("[green]Settings updated.[/green]")


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
        "8": lambda: action_settings(s, console),
    }
    while True:
        console.print()
        console.print(_status_panel(s))
        console.print(_menu_table(s))
        choice = _ask(console, "Choose")
        if choice in ("0", "q", "quit", "exit", ""):
            console.print("[dim]Bye.[/dim]")
            return 0
        action = actions.get(choice)
        if action is None:
            console.print(f"[red]Unknown choice '{choice}'.[/red]")
            continue
        if choice in ("3", "4", "5", "6", "7") and not s.has_data:
            console.print("[yellow]Collect from the devices first (option 2).[/yellow]")
            continue
        try:
            action()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
        except Exception as exc:  # noqa: BLE001 - never kill the menu on one action
            console.print(f"[red]{exc.__class__.__name__}: {exc}[/red]")
