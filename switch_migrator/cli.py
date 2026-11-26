"""Command-line entry point: flags in, the workflow run, an exit code out.

All the work lives in `switch_migrator.workflow`; this module only turns the
flags into its option types, checks them before any switch is touched, and
prints. The interactive menu drives the same workflow.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from rich.console import Console

from switch_migrator import (
    __version__,
    cabling_sheet,
    handover,
    health,
    location,
    mac_check,
    mlt_generate,
    paths,
    standard,
    verify,
    version_check,
)
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
from switch_migrator.connection import enable_legacy_ssh_algorithms
from switch_migrator.menu import MenuStart, run_menu
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator.plan import Layout as PlanLayout
from switch_migrator.plan import pins_from_sheet, uids_from_sheet
from switch_migrator.report import console as console_report
from switch_migrator.report.migration import new_switch_names
from switch_migrator.report.progress import make_progress
from switch_migrator.workflow.collect import (
    CollectContext,
    Collected,
    CollectOptions,
    collect,
    make_runner,
    preview_commands,
    render_dry_run,
)
from switch_migrator.workflow.macs import after_recorder, mac_check_targets, run_mac_check
from switch_migrator.workflow.outputs import OutputOptions, write_outputs, write_rack_sheet
from switch_migrator.workflow.report import PlanSetup, ReportOptions, build_report

log = logging.getLogger("switch_migrator")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switch-migrator",
        usage="%(prog)s [collect | plan | verify SHEET] [options]",
        description="Migration toolkit for Extreme VOSS/ERS switches onto an SPB "
                    "fabric. Read-only: only 'show' commands are ever sent.",
        epilog="The three verbs of a migration, each a shorthand for flags:\n"
               "  collect        read the old switches once, keep a snapshot\n"
               "                 (= --migration-sheets --save-snapshot)\n"
               "  plan           place every link on the new switches, write the\n"
               "                 sheets and the new configs (= --plan; add\n"
               "                 --from-snapshot FILE to plan without SSH)\n"
               "  verify SHEET   after the window: check every moved link\n"
               "                 (= --verify-migration SHEET)\n"
               "No arguments at all opens the interactive menu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", type=Path,
                        help="config file (default: the config.yaml in the "
                             "tool's home directory, --where shows which)")
    parser.add_argument("-i", "--inventory", type=Path,
                        help="inventory YAML with the switches to migrate")
    parser.add_argument("-s", "--switch", action="append", default=[],
                        metavar="NAME:PLATFORM[:HOST]",
                        help="add a switch on the CLI, e.g. old-sw-01:ers "
                             "(repeatable, combines with -i)")
    parser.add_argument("-o", "--output-dir", type=Path,
                        help="directory for reports and logs (default: "
                             "<home>/output, or $SM_OUTPUT_DIR; --where shows "
                             "which)")
    parser.add_argument("--no-fabric", action="store_true",
                        help="inventory/state report only: read and report each "
                             "switch's port/MLT/IST/VLAN state and do NOT collect "
                             "or compare against any DvR fabric. For isolated "
                             "environments whose VLANs/I-SIDs are intentionally "
                             "not in the fabric. DvR controllers and I-SID "
                             "conventions become optional in the config.")
    parser.add_argument("--extract-config", action="store_true",
                        help="also pull each VOSS switch's running-config and "
                             "write a neutralized, migration-ready extract "
                             "(port/MLT/VLAN/I-SID only, secrets & identity "
                             "removed, uplinks annotated) to "
                             "<output>/config/<device>.cfg. Review before use.")
    parser.add_argument("--migration-sheets", action="store_true",
                        help="add the migration-day deliverables: a per-port "
                             "info sheet, a DC cabling sheet (connected ports, "
                             "with empty NEW switch/port columns) and a "
                             "commands file with per-port MAC checks for the "
                             "new switch. Combine with --extract-config to "
                             "include the device config in that file.")
    parser.add_argument("--new-switch", metavar="NAME[,NAME...]", default="",
                        help="the new switch(es): A,B for one vIST pair, "
                             "A,B,C,D for two. The cabling sheet offers them in "
                             "a dropdown; --plan fills them pair by pair.")
    parser.add_argument("--plan", action="store_true",
                        help="plan where every old link goes on the --new-switch "
                             "pair(s) and pre-fill the cabling sheet: dual-homed "
                             "links from port 1 up (same port on both peers, MLT "
                             "4xx), single-homed from the last access port down, "
                             "the last --new-uplinks ports never. Implies "
                             "--migration-sheets; works with --from-snapshot.")
    parser.add_argument("--new-ports", type=int, metavar="N",
                        help="--plan: ports on the new switch model (config "
                             "plan.ports)")
    parser.add_argument("--new-uplinks", type=int, metavar="N",
                        help="--plan: how many of the LAST ports are fabric "
                             "uplinks and never planned (config plan.uplinks)")
    parser.add_argument("--pins", type=Path, metavar="SHEET",
                        help="--plan: keep every NEW switch/port an edited "
                             "cabling sheet already has, and plan the rest "
                             "around them")
    parser.add_argument("--split-by-location", action="store_true",
                        help="give the cabling sheet one worksheet per "
                             "location instead of one for everything, so each "
                             "site's technicians get only their own links. "
                             "The location comes from the switch name (see the "
                             "'locations' section of the config).")
    parser.add_argument("--location-group", action="append", default=[],
                        metavar="NAME=LOC1,LOC2",
                        help="for this run only, put these locations on one "
                             "worksheet, e.g. --location-group "
                             "'Frankfurt=gx-11,gx-12'. Repeatable; replaces "
                             "the groups from the config. Implies "
                             "--split-by-location.")
    parser.add_argument("--csv", action="store_true",
                        help="additionally export the tables as CSV files")
    parser.add_argument("--no-excel", action="store_true",
                        help="skip the Excel report")
    parser.add_argument("--save-raw", action="store_true",
                        help="save the raw CLI output of every command under "
                             "<output>/raw/<device>/ (for troubleshooting and "
                             "--offline replays)")
    parser.add_argument("--offline", type=Path, metavar="RAW_DIR",
                        help="don't SSH anywhere; replay raw outputs previously "
                             "captured with --save-raw")
    parser.add_argument("--dry-run", action="store_true",
                        help="connect to nothing and print every command the "
                             "run would send to each device, then exit. Use it "
                             "to show a change board exactly what the tool "
                             "does - the list is produced by the real "
                             "collection code, not a written-down copy.")
    parser.add_argument("--save-snapshot", nargs="?", const="", metavar="FILE",
                        help="write the complete collected state to a JSON "
                             "snapshot (default: <output>/snapshot-<stamp>.json) "
                             "so any report can be regenerated later without "
                             "touching the switches again")
    parser.add_argument("--from-snapshot", type=Path, metavar="FILE",
                        help="produce the reports from a saved snapshot instead "
                             "of collecting: no SSH, no credentials, no load on "
                             "the devices")
    parser.add_argument("--manifest", action="store_true",
                        help="write a run manifest (<output>/manifest-<stamp>.json): "
                             "every command sent and its outcome, what failed, "
                             "which files were produced - an audit trail of the "
                             "pre-migration check")
    parser.add_argument("--handover", action="store_true",
                        help="collect this run's output into "
                             "<output>/handover-<stamp>/ with an index.html "
                             "tying it together - the folder to hand to a "
                             "reviewer, a change record or your future self")
    parser.add_argument("--health-check", action="store_true",
                        help="go/no-go check before the migration window: "
                             "flags anything already broken that the migration "
                             "would make worse (degraded MLTs, a down vIST, "
                             "lost uplink redundancy, VLANs with nowhere to "
                             "land). Sends no extra commands - it is derived "
                             "from the same collection.")
    parser.add_argument("--verify-migration", type=Path, metavar="SHEET",
                        help="after the window: read the filled-in cabling "
                             "sheet (.xlsx or .csv) and check every re-patched "
                             "link on the NEW switches - link up, expected MACs "
                             "back, LLDP neighbor, VLANs and MLT membership. "
                             "The switches named here are the ones to collect.")
    parser.add_argument("--mac-snapshot", type=Path, metavar="DIR",
                        help="also save a migration-check snapshot of every "
                             "switch read in this run, on the same sessions - "
                             "the baseline for --mac-baseline. Take it of the "
                             "OLD switches before the window (the day before "
                             "and right before is better than once). DIR must "
                             "not exist yet.")
    parser.add_argument("--mac-baseline", type=Path, action="append",
                        default=[], metavar="DIR",
                        help="with --verify-migration: also check every "
                             "end-device MAC the old switches learned - back "
                             "anywhere, right VLAN, on the port the sheet "
                             "says? DIR is a --mac-snapshot folder, a "
                             "migration-check snapshot, or a --save-raw folder "
                             "of old ERS switches taken with --migration-sheets. "
                             "Repeatable: several baselines are merged.")
    parser.add_argument("--mac-watch", action="store_true",
                        help="with --mac-baseline: read the new switches again "
                             "every interval until every MAC is back or the "
                             "number stops falling (config mac_check: "
                             "interval / rounds / stable; default 120 s, 15 "
                             "rounds, 3)")
    parser.add_argument("--sheet-name", metavar="NAME", default="",
                        help="worksheet to read from the .xlsx given to "
                             "--verify-migration / --generate-mlt / "
                             "--rack-sheet (default: "
                             "the 'Cabling' sheet)")
    parser.add_argument("--generate-mlt", type=Path, metavar="SHEET",
                        help="generate the VOSS MLT config blocks for the new "
                             "switches from a filled-in cabling sheet, to "
                             "<output>/config/mlt-blocks.cfg. Reads no device.")
    parser.add_argument("--rack-sheet", type=Path, metavar="SHEET",
                        help="print version of a filled-in cabling sheet for "
                             "the technicians: one page per old switch, in "
                             "faceplate order, what is on each port and where "
                             "it goes, a box to tick. Writes "
                             "<output>/rack-sheet-<stamp>.xlsx. Reads no device.")
    parser.add_argument("--no-smlt", action="store_true",
                        help="--generate-mlt: emit plain single-switch MLTs "
                             "instead of SMLT pairs")
    parser.add_argument("--profile", metavar="NAME",
                        help="apply a named profile from the profiles file: "
                             "inventory, output dir, new switch, save-raw, "
                             "manifest, offline replay, location groups. Any "
                             "flag given explicitly on the command line wins "
                             "over the profile's value.")
    parser.add_argument("--profiles-file", type=Path,
                        help="where the named profiles live (default: the "
                             "profiles.yaml in the tool's home directory)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print the ports/MLTs/fabric tables to the console")
    parser.add_argument("--debug", action="store_true",
                        help="debug logging (includes netmiko)")
    parser.add_argument("--menu", action="store_true",
                        help="open the interactive toolkit menu (also the "
                             "default when no arguments are given): select "
                             "switches, collect once, then produce any output "
                             "from that same data")
    parser.add_argument("--where", action="store_true",
                        help="print the home directory the config, the "
                             "inventory and the profiles come from, where the "
                             "reports go, and every place that was searched - "
                             "then exit. Reads nothing else.")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def setup_logging(output_dir: Path, debug: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(output_dir / "switch-migrator.log")
    ]
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.DEBUG if debug else logging.WARNING)
    handlers.append(stderr)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    if not debug:
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger("netmiko").setLevel(logging.WARNING)



# --------------------------------------------------------------------------
# where the files are
# --------------------------------------------------------------------------

# the paths that name a file to READ. A relative one is looked up in the
# working directory first and in the tool's home second (paths.input_path);
# the ones that name something to WRITE (-o, --mac-snapshot, --save-snapshot)
# stay exactly as typed, because a run writes where it was told to.
_INPUT_PATHS = ("inventory", "offline", "from_snapshot", "pins",
                "verify_migration", "generate_mlt", "rack_sheet")


def _resolve_paths(args: argparse.Namespace) -> None:
    """Fill in the paths the command line left open from the tool's home, and
    find the relative ones it did name (paths.py).

    Nothing has been read yet when this runs, so the profile, the log file and
    every report agree on one set of paths - whatever directory the tool was
    started from. --output-dir is left for later: a profile may still set it.
    """
    args.config = (paths.input_path(args.config) if args.config
                   else paths.config_file())
    args.profiles_file = (paths.input_path(args.profiles_file) if args.profiles_file
                          else paths.profiles_file())
    for attr in _INPUT_PATHS:
        given = getattr(args, attr)
        if given is not None:
            setattr(args, attr, paths.input_path(given))
    args.mac_baseline = [paths.input_path(p) for p in args.mac_baseline]


def _print_where(args: argparse.Namespace, console: Console) -> int:
    """--where: the resolved paths, and every place that was searched. Reads
    no config, creates no directory - it answers 'which config is it using?'
    when something is missing, which is exactly when nothing else may run."""
    home = paths.home()
    console.print(f"[bold]switch-migrator {__version__}[/bold]")
    rows = [
        ("home", home, "holds config.yaml" if (home / paths.CONFIG_NAME).is_file()
                       else "[yellow]no config.yaml here yet[/yellow]"),
        ("config", args.config, "" if args.config.is_file() else "[yellow]missing[/yellow]"),
        ("inventory", paths.inventory_file(),
         "used when neither -i nor the config names one"),
        ("profiles", args.profiles_file,
         "" if args.profiles_file.is_file() else "none yet"),
        ("output", args.output_dir or paths.output_root(), ""),
    ]
    # soft_wrap: a path cut in half by the terminal width is no answer
    for name, value, note in rows:
        console.print(f"  {name:<10} {value}" + (f"  [dim]{note}[/dim]" if note else ""),
                      soft_wrap=True)
    for i, place in enumerate(paths.looked_in()):
        console.print(f"  [dim]{'searched' if i == 0 else '':<10} {place}[/dim]",
                      soft_wrap=True)
    for var in (paths.HOME_ENV, paths.OUTPUT_ENV):
        set_to = os.environ.get(var)
        console.print(f"  [dim]{var:<13}{set_to if set_to else '(not set)'}[/dim]",
                      soft_wrap=True)
    return 0


# --------------------------------------------------------------------------
# checks before the run
# --------------------------------------------------------------------------

def _load_sheet(path: Path, args: argparse.Namespace) -> cabling_sheet.Sheet:
    return cabling_sheet.load(path, sheet_name=args.sheet_name or None)


def _targets_from_sheet(args: argparse.Namespace,
                        console: Console) -> list[SwitchTarget]:
    """The switches to verify are the ones the sheet says links moved to."""
    try:
        sheet = _load_sheet(args.verify_migration, args)
    except cabling_sheet.SheetError as exc:
        raise ConfigError(str(exc)) from None
    names = sorted(sheet.by_new_switch())
    if not names:
        raise ConfigError(
            f"{args.verify_migration}: no row has both a NEW switch and a NEW "
            f"port yet, so there is nothing to verify")
    console.print(f"Verifying against {len(names)} new switch(es) named in the "
                  f"sheet: {', '.join(names)} [dim](assumed VOSS; use -s to "
                  f"say otherwise)[/dim]")
    return [SwitchTarget(name=n, host=n, platform=Platform.VOSS) for n in names]


def _check_mac_args(args: argparse.Namespace, from_snapshot: Path | None,
                    console: Console | None = None) -> None:
    """--mac-snapshot / --mac-baseline / --mac-watch: everything that can be
    wrong, before any switch is read."""
    if not (args.mac_snapshot or args.mac_baseline or args.mac_watch):
        return
    if args.mac_watch and not args.mac_baseline:
        raise ConfigError("--mac-watch goes with --mac-baseline")
    if args.mac_baseline and not args.verify_migration:
        raise ConfigError(
            "--mac-baseline goes with --verify-migration: it is the BEFORE side, and "
            "the verification reads the AFTER side off the new switches. To compare "
            "two folders you already have, with no switch touched, use the menu's "
            "'Compare MAC snapshots' or `migration-check diff --before DIR --after DIR` "
            "(same install)")
    if args.mac_snapshot and args.verify_migration:
        raise ConfigError("--mac-snapshot records a baseline of the OLD switches; "
                          "a verification with --mac-baseline saves the new "
                          "switches' snapshot by itself (mac-check-<stamp>/after)")
    if from_snapshot or args.generate_mlt:
        raise ConfigError("the MAC options read switches; they cannot run from a "
                          "saved snapshot or with --generate-mlt")
    if args.mac_snapshot and args.mac_snapshot.exists() and (
            not args.mac_snapshot.is_dir() or any(args.mac_snapshot.iterdir())):
        raise ConfigError(f"--mac-snapshot: {args.mac_snapshot} exists and is not "
                          f"an empty folder - a snapshot is never overwritten")
    missing = [str(p) for p in args.mac_baseline if not p.is_dir()]
    if missing:
        raise ConfigError(f"--mac-baseline: no such folder: {', '.join(missing)}")
    unreadable = mac_check.unreadable_baselines(list(args.mac_baseline))
    if unreadable and console is not None:
        console.print(
            f"[bold yellow]MAC baseline:[/bold yellow] {len(unreadable)} old VOSS "
            f"folder(s) hold only 'show interfaces gigabitEthernet fdb-entry', which "
            f"migration-check cannot read - their MACs are left out of the "
            f"comparison: {', '.join(unreadable)}. Take VOSS baselines with "
            f"--mac-snapshot.")


def _check_plan_args(args: argparse.Namespace, cfg: Config) -> PlanSetup:
    """--plan: the new switches and their layout, and the pins. Everything that can
    be wrong is found before any switch is read."""
    args.migration_sheets = True
    names = new_switch_names(args.new_switch)
    if not names:
        raise ConfigError("--plan needs the new switches: --new-switch A,B "
                          "(a vIST pair; A,B,C,D for two pairs)")
    if len(set(names)) != len(names):
        raise ConfigError(f"--new-switch names a switch twice: {args.new_switch}")
    ports = args.new_ports if args.new_ports is not None else cfg.plan.ports
    uplinks = args.new_uplinks if args.new_uplinks is not None else cfg.plan.uplinks
    if ports is None or uplinks is None:
        raise ConfigError("--plan needs the new model's layout: --new-ports N "
                          "--new-uplinks N (or plan: {ports: 52, uplinks: 4} in "
                          "config.yaml) - the last N ports are the fabric uplinks")
    try:
        layout = PlanLayout(ports=ports, uplinks=uplinks)
        standard.mlt_name_for_new_lag("1/1", cfg.plan.mlt_name)
    except (ValueError, standard.ConventionError) as exc:
        raise ConfigError(f"--plan: {exc}") from None
    if not args.pins:
        return PlanSetup(layout)
    try:
        rows = _load_sheet(args.pins, args).rows
    except cabling_sheet.SheetError as exc:
        raise ConfigError(f"--pins: {exc}") from None
    return PlanSetup(layout, pins_from_sheet(rows), uids_from_sheet(rows))


def _run_rack_sheet(args: argparse.Namespace, console: Console) -> int:
    """--rack-sheet: filled-in cabling sheet in, printable rack sheet out."""
    try:
        sheet = _load_sheet(args.rack_sheet, args)
    except cabling_sheet.SheetError as exc:
        console.print(f"[bold red]Cabling sheet:[/bold red] {exc}")
        return 2
    path = write_rack_sheet(sheet, args.output_dir)
    for problem in sheet.problems:
        console.print(f"[yellow]{problem}[/yellow]")
    console.print(f"Rack sheet: {len(sheet.migrated)} of {len(sheet.rows)} link(s) "
                  f"have a new port planned. Written: [bold]{path}[/bold]")
    return 0


def _run_generate_mlt(args: argparse.Namespace, console: Console) -> int:
    """--generate-mlt: cabling sheet in, MLT config blocks out. No device."""
    try:
        sheet = _load_sheet(args.generate_mlt, args)
    except cabling_sheet.SheetError as exc:
        console.print(f"[bold red]Cabling sheet:[/bold red] {exc}")
        return 2
    result = mlt_generate.plan(sheet, smlt=not args.no_smlt)
    text = mlt_generate.render(result, source=str(args.generate_mlt))
    path = args.output_dir / "config" / "mlt-blocks.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    for problem in sheet.problems:
        console.print(f"[yellow]{problem}[/yellow]")
    for problem in result.problems:
        console.print(f"[bold yellow]![/bold yellow] {problem}")
    per_switch = result.by_switch
    console.print(f"[bold]{len(result.plans)} MLT(s)[/bold] across "
                  f"{len(per_switch)} new switch(es) from "
                  f"{len(sheet.migrated)} re-patched link(s)")
    for switch, plans in sorted(per_switch.items()):
        for p in plans:
            flags = " ".join(f for f, on in (("smlt", p.smlt), ("lacp", p.lacp),
                                             ("flex-uni", p.flex_uni)) if on)
            console.print(f"  {switch}  MLT {p.mlt_id} \"{p.name}\"  "
                          f"members {','.join(p.members)}  [dim]{flags}[/dim]")
    console.print(f"Written: [bold]{path}[/bold]")
    console.print("[dim]Review before pasting - LACP and the SMLT peer are "
                  "the two things this cannot know from the sheet alone.[/dim]")
    return 1 if result.problems else 0


def _render_health(report: health.HealthReport, console: Console) -> None:
    counts = report.counts()
    verdict = report.verdict
    color = {health.BLOCK: "bold red", health.WARN: "bold yellow",
             health.OK: "bold green"}.get(verdict, "bold")
    console.print(f"\n[{color}]Pre-migration health: {verdict}[/{color}] - "
                  f"{counts[health.BLOCK]} switch(es) blocked, "
                  f"{counts[health.WARN]} with warnings, "
                  f"{counts[health.OK]} clean")
    for f in report.blockers:
        console.print(f"  [red]BLOCK[/red] {f.switch} {f.check}: {f.detail}")
        if f.action:
            console.print(f"        [dim]{f.action}[/dim]")


def _apply_cli_profile(args: argparse.Namespace, console: Console) -> None:
    """--profile NAME: fill in run values from the named profile, but only
    where the command line kept the parser default - an explicit flag always
    wins over the profile."""

    try:
        profiles = profiles_mod.load_profiles(args.profiles_file)
    except profiles_mod.ProfileError as exc:
        raise ConfigError(str(exc)) from None
    prof = profiles.get(args.profile)
    if prof is None:
        known = ", ".join(sorted(profiles)) or "(none)"
        raise ConfigError(f"no profile '{args.profile}' in "
                          f"{args.profiles_file} - known: {known}")
    if "inventory" in prof and args.inventory is None:
        args.inventory = prof["inventory"]
    if "output_dir" in prof and args.output_dir is None:
        args.output_dir = prof["output_dir"]
    if "offline" in prof and args.offline is None:
        args.offline = prof["offline"]
    if "new_switch" in prof and not args.new_switch:
        args.new_switch = prof["new_switch"]
    for key, attr in (("save_raw", "save_raw"), ("manifest", "manifest"),
                      ("split_by_location", "split_by_location")):
        if prof.get(key) and not getattr(args, attr):
            setattr(args, attr, True)
    if prof.get("auto_snapshot") and args.save_snapshot is None:
        args.save_snapshot = ""        # the default-name sentinel
    if prof.get("location_groups") and not args.location_group:
        args.location_group = [f"{g}={','.join(m)}"
                               for g, m in prof["location_groups"].items()]
    console.print(f"[dim]Profile '{args.profile}' applied from "
                  f"{args.profiles_file}.[/dim]")


# --------------------------------------------------------------------------
# flags -> workflow options
# --------------------------------------------------------------------------

def _collect_options(args: argparse.Namespace) -> CollectOptions:
    return CollectOptions(
        reads=Reads.for_run(extract_config=args.extract_config,
                            migration_sheets=args.migration_sheets,
                            verify=bool(args.verify_migration)),
        fabric=not args.no_fabric, offline=args.offline, dry_run=args.dry_run,
        raw_dir=(args.output_dir / "raw") if args.save_raw else None)


def _report_options(args: argparse.Namespace, cfg: Config, run: _Run) -> ReportOptions:
    split = None
    if args.migration_sheets and (args.split_by_location or args.location_group):
        split = cfg.locations
        if args.location_group:
            # a per-run scope replaces the config's groups, but keeps the name
            # patterns - those describe the estate, not this window
            split = replace(split, groups=location.parse_group_args(args.location_group))
    return ReportOptions(
        fabric_checked=not args.no_fabric, migration_sheets=args.migration_sheets,
        new_switches=new_switch_names(args.new_switch), plan=run.plan,
        split=split, health=args.health_check, verify_sheet=run.verify_sheet)


def _output_options(args: argparse.Namespace, stamp: str) -> OutputOptions:
    return OutputOptions(output_dir=args.output_dir, stamp=stamp,
                         excel=not args.no_excel, csv=args.csv,
                         extract_config=args.extract_config,
                         commands=args.migration_sheets, new_switch=args.new_switch)


@dataclass
class _Run:
    """What the checks before the run produced."""
    cfg: Config
    targets: list[SwitchTarget]
    creds: Credentials
    dvr_creds: Credentials
    plan: PlanSetup | None = None
    verify_sheet: cabling_sheet.Sheet | None = None


def _prepare(args: argparse.Namespace, console: Console) -> _Run:
    """Everything that can be wrong, found before any switch is touched."""
    from_snapshot = args.from_snapshot
    # generating MLT blocks reads a sheet, not a fabric; a snapshot run
    # compares nothing. Neither needs the DvR/I-SID sections to be present.
    cfg = load_config(args.config,
                      require_fabric=(not args.no_fabric and not from_snapshot
                                      and not args.generate_mlt))
    targets = _named_targets(args, cfg, console)
    if args.location_group:
        try:
            location.parse_group_args(args.location_group)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    verify_sheet = None
    if args.verify_migration:
        try:
            verify_sheet = _load_sheet(args.verify_migration, args)
        except cabling_sheet.SheetError as exc:
            raise ConfigError(str(exc)) from None
        if not targets:
            # the sheet already names the switches to check: the new ones the
            # links were moved onto. They are fabric leaves, hence VOSS.
            targets = _targets_from_sheet(args, console)
    _check_mac_args(args, from_snapshot, console)
    if args.save_snapshot:          # an explicit file name, not the '' sentinel
        try:
            snapshot_mod.check_target(Path(args.save_snapshot))
        except snapshot_mod.SnapshotError as exc:
            raise ConfigError(str(exc)) from None
    plan = _check_plan_args(args, cfg) if args.plan else None
    if not targets and not from_snapshot and not args.generate_mlt:
        raise ConfigError("no switches given: use -i inventory.yaml and/or "
                          "-s NAME:PLATFORM[:HOST]")
    dupes = sorted(n for n, c in Counter(t.name for t in targets).items() if c > 1)
    if dupes:
        raise ConfigError(f"duplicate switch names: {', '.join(dupes)}")
    if (args.split_by_location or args.location_group) and not args.migration_sheets:
        console.print("[yellow]--split-by-location/--location-group only affect the "
                      "cabling sheet - add --migration-sheets for them to have any "
                      "effect.[/yellow]")
    creds, dvr_creds = _credentials(args, cfg)
    return _Run(cfg, targets, creds, dvr_creds, plan, verify_sheet)


def _named_targets(args: argparse.Namespace, cfg: Config,
                   console: Console) -> list[SwitchTarget]:
    """The switches named by -i/-s, or the default inventory when none are."""
    if not args.inventory and not args.switch and not args.from_snapshot \
            and not args.generate_mlt and not args.verify_migration:
        # no switches named anywhere: fall back to the config's default
        # inventory (or a ./switches.yaml), so routine runs need no -i
        fallback = default_inventory(cfg)
        if fallback is not None:
            args.inventory = fallback
            console.print(f"[dim]Using default inventory: {fallback}[/dim]")
    targets: list[SwitchTarget] = []
    if args.inventory:
        targets.extend(load_inventory(args.inventory))
    targets += [parse_switch_arg(raw) for raw in args.switch]
    return targets


def _credentials(args: argparse.Namespace,
                 cfg: Config) -> tuple[Credentials, Credentials]:
    """Switch and DvR credentials; nothing is prompted for a run that connects
    to nothing."""
    if args.offline or args.dry_run or args.generate_mlt or args.from_snapshot:
        offline = Credentials("offline", "offline")
        return offline, offline
    creds = get_credentials("switches", "SM")
    # no-fabric mode never touches a DvR, so don't prompt for its creds
    dvr_creds = creds if args.no_fabric else get_credentials(
        "DvR controllers", "SM_DVR", fallback=creds)
    if cfg.ssh.legacy_algorithms:
        # initialize once on the main thread before any worker connects
        enable_legacy_ssh_algorithms()
    return creds, dvr_creds


# --------------------------------------------------------------------------
# the runs
# --------------------------------------------------------------------------

def _run_dry_run(args: argparse.Namespace, run: _Run, console: Console) -> int:
    mac_for: set[str] = set()
    if args.mac_snapshot:
        mac_for = {t.name for t in run.targets}
    elif args.mac_baseline and run.verify_sheet is not None:
        mac_for = {t.name for t in mac_check_targets(run.targets, run.verify_sheet)}
    render_dry_run(preview_commands(
        run.targets, run.cfg, _collect_options(args),
        dvrs=[] if args.no_fabric else run.cfg.dvr_controllers, mac_for=mac_for),
        console)
    return 0


def _collect_or_load(args: argparse.Namespace, run: _Run, opts: CollectOptions,
                     recorder: mac_check.SnapshotRecorder | None,
                     console: Console) -> Collected | int:
    """The state to report on: from the devices, or from a snapshot. An int is
    an exit code - the run cannot go on."""
    if args.from_snapshot:
        try:
            audits, fabric, meta = snapshot_mod.load(args.from_snapshot)
        except snapshot_mod.SnapshotError as exc:
            console.print(f"[bold red]Snapshot error:[/bold red] {exc}")
            return 2
        console.print(f"[bold]switch-migrator {__version__}[/bold] - "
                      f"{snapshot_mod.describe(meta, audits)}")
        console.print("[yellow]No device was contacted: reporting from the "
                      "saved snapshot.[/yellow]")
        return Collected(audits, fabric)

    if args.no_fabric:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"inventory of {len(run.targets)} switch(es) "
                      f"[yellow](--no-fabric: no DvR comparison)[/yellow]")
    else:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"audit of {len(run.targets)} switch(es) against "
                      f"{len(run.cfg.dvr_controllers)} DvR controller(s)")
    dvrs = 0 if args.no_fabric else len(run.cfg.dvr_controllers)
    with make_progress(console, len(run.targets), dvrs) as progress:
        collected = collect(run.targets, run.creds, run.dvr_creds,
                            CollectContext(run.cfg, opts, progress, recorder))
    if args.mac_snapshot and recorder is not None:
        console.print(f"MAC snapshot (migration-check baseline): "
                      f"[bold]{recorder.path}[/bold]")
    if not args.no_fabric:
        fabric = collected.fabric
        if not fabric.dvrs_ok:
            console.print("[bold red]No DvR controller could be read - aborting, "
                          "there is no fabric state to compare against.[/bold red]")
            for err in fabric.dvr_errors:
                console.print(f"  [red]{err}[/red]")
            console.print("[dim]The switches were read successfully; re-run with "
                          "--no-fabric for an inventory report without the fabric "
                          "comparison.[/dim]")
            return 1
        console.print(f"Fabric state: {len(fabric.isids)} I-SIDs from "
                      f"{', '.join(fabric.dvrs_ok)}"
                      + (f" [yellow]({len(fabric.dvr_errors)} DvR error(s))[/yellow]"
                         if fabric.dvr_errors else ""))
    return collected


def _print_verification(report: verify.VerifyReport, console: Console) -> None:
    counts = report.counts()
    color = "bold red" if counts[verify.FAIL] else (
        "bold yellow" if counts[verify.WARN] else "bold green")
    console.print(f"\n[{color}]Migration verification:[/{color}] "
                  f"{counts[verify.PASS]} pass, {counts[verify.WARN]} warn, "
                  f"{counts[verify.FAIL]} fail, "
                  f"{counts[verify.PENDING]} not migrated yet")
    for problem in report.problems:
        console.print(f"  [yellow]{problem}[/yellow]")
    for v in report.verdicts:
        if v.result == verify.FAIL:
            console.print(f"  [red]FAIL[/red] {v.uid} {v.old_switch} "
                          f"{v.old_port} -> {v.new_switch} {v.new_port}: {v.why}")


def _run(args: argparse.Namespace, run: _Run, console: Console) -> int:
    started = datetime.now()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    opts = _collect_options(args)
    try:
        recorder = _mac_recorder(args, run, stamp)
    except Exception as exc:  # broad on purpose: checked above; this is the unexpected rest
        console.print(f"[bold red]MAC snapshot could not be started:[/bold red] {exc}")
        return 2

    collected = _collect_or_load(args, run, opts, recorder, console)
    if isinstance(collected, int):
        return collected
    audits, fabric = collected.audits, collected.fabric
    _print_unreachable(audits, console)

    report = build_report(audits, fabric, run.cfg, _report_options(args, run.cfg, run),
                          console)
    console_report.render(report.tables, Console(), verbose=args.verbose)
    if report.health is not None:
        _render_health(report.health, console)
    if report.verify is not None:
        _print_verification(report.verify, console)

    written: list[Path] = []
    mac_check_failed = False
    sheet = run.verify_sheet
    if args.mac_baseline and report.verify is not None and recorder is not None \
            and sheet is not None:
        # later --mac-watch rounds re-read the new switches on fresh sessions;
        # without the raw capture, so a round never overwrites the run's own
        rounds = replace(opts, raw_dir=None)
        try:
            job = mac_check.MacCheckJob(args.mac_baseline, recorder.path, sheet,
                                        args.output_dir, stamp)
            watch = mac_check.Watching(
                run.cfg.mac_check, run.targets,
                lambda t: make_runner(t, t.platform, run.creds, run.cfg, rounds),
                run.cfg.ssh.workers) if args.mac_watch else None
            check = run_mac_check(job, watch, console)
        except Exception as exc:  # broad on purpose: the other reports must still be written
            mac_check_failed = True
            log.exception("MAC check failed")
            console.print(f"[bold red]MAC check could not run:[/bold red] {exc}")
        else:
            written += [check.text, check.json]

    written += write_outputs(report, audits, run.cfg, _output_options(args, stamp),
                             console)
    if args.save_snapshot is not None and not args.from_snapshot:
        written.append(_save_snapshot(args, audits, fabric, started, stamp))
    for path in written:
        console.print(f"Report written: [bold]{path}[/bold]")

    # a MAC not back yet is a warning, like 'no MAC yet' in the verification; a
    # check that could not run at all is an error a script must see
    code = report.exit_code(audits, fabric, extra_failure=mac_check_failed)

    if args.manifest:
        path = manifest_mod.write(
            args.output_dir / f"manifest-{stamp}.json",
            manifest_mod.build(audits, fabric, manifest_mod.RunRecord(
                args, started, datetime.now(), written, collected.commands,
                config_path=args.config, exit_code=code)))
        console.print(f"Run manifest: [bold]{path}[/bold]")
        written.append(path)
    # last, so the folder contains everything the run produced - the manifest
    # included, which is the file a change record actually wants
    if args.handover:
        bundle = handover.build(args.output_dir, handover.Contents(
            written, report.tables, audits,
            meta={"started": started.isoformat(timespec="seconds"),
                  "config": str(args.config),
                  "switches": ", ".join(a.name for a in audits)},
            stamp=stamp))
        console.print(f"Handover bundle: [bold]{bundle}[/bold]\n"
                      f"  open [bold]{bundle / 'index.html'}[/bold]")
    return code


def _mac_recorder(args: argparse.Namespace, run: _Run,
                  stamp: str) -> mac_check.SnapshotRecorder | None:
    """What records the MAC snapshot during this run's collection, if anything."""
    if args.mac_snapshot:
        return mac_check.SnapshotRecorder(args.mac_snapshot,
                                          label=args.mac_snapshot.name)
    if args.mac_baseline and run.verify_sheet is not None:
        return after_recorder(args.output_dir, stamp, args.mac_watch,
                              run.targets, run.verify_sheet)
    return None


def _print_unreachable(audits: list[SwitchAudit], console: Console) -> None:
    """Unreachable devices go to the report too, but say it loudly right away."""
    for audit in audits:
        if not audit.reachable:
            reason = audit.errors[-1] if audit.errors else "unknown error"
            console.print(f"[bold red]UNREACHABLE[/bold red] {audit.name} "
                          f"({audit.host}): {reason}")


def _save_snapshot(args: argparse.Namespace, audits: list[SwitchAudit],
                   fabric: FabricState, started: datetime, stamp: str) -> Path:
    # '--save-snapshot' on its own carries the empty sentinel -> default
    # name in the output dir; '--save-snapshot FILE' names it explicitly
    args.save_snapshot = (Path(args.save_snapshot) if args.save_snapshot
                          else args.output_dir / f"snapshot-{stamp}.json")
    return snapshot_mod.save(
        args.save_snapshot, audits, fabric,
        meta={"started": started.isoformat(timespec="seconds"),
              "config": str(args.config), "no_fabric": bool(args.no_fabric)})


def _warn_if_stale(console: Console) -> None:
    """Say so when this checkout is behind its upstream (as of the last fetch)."""
    state = version_check.checkout_state()
    message = state.warning() if state is not None else ""
    if message:
        console.print(f"[bold yellow]{message}[/bold yellow]")


# collect / plan / verify: the three things a migration does, as shorthands for
# the flags that do them (council review, Field Engineer 5)
VERBS = {
    # --save-snapshot takes an OPTIONAL value, so it must never be the LAST
    # flag of an expansion: `collect switches.yaml` would make the inventory
    # the snapshot's file name and write the collected state over it
    "collect": ["--save-snapshot", "--migration-sheets"],
    "plan": ["--plan"],
    "verify": ["--verify-migration"],      # takes the sheet as its argument
}


def expand_verb(argv: list[str]) -> list[str]:
    """'plan -i x.yaml ...' -> '--plan -i x.yaml ...'. Flags-only command lines
    pass through untouched, so every existing script keeps working."""
    if argv and argv[0] in VERBS:
        return [*VERBS[argv[0]], *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    raw_argv = expand_verb(sys.argv[1:] if argv is None else argv)
    args = build_arg_parser().parse_args(raw_argv)
    console = Console(stderr=True)

    # the tool runs from anywhere on the server, so every path it was not
    # given comes from its home directory, not from the working directory
    _resolve_paths(args)
    if args.where:
        return _print_where(args, console)

    # The profile can set output_dir, and the log file lives inside it - so it
    # has to be applied BEFORE logging is set up, or the reports go to the
    # profile's directory and their log to the default one.
    if args.profile:
        try:
            _apply_cli_profile(args, console)
        except ConfigError as exc:
            console.print(f"[bold red]Config error:[/bold red] {exc}")
            return 2
    if args.output_dir is None:
        args.output_dir = paths.output_root()
    setup_logging(args.output_dir, args.debug)
    _warn_if_stale(console)

    # No arguments at all (or an explicit --menu): open the interactive toolkit
    # menu. Every flag keeps working exactly as before.
    if args.menu or not raw_argv:
        # the profile is handed on by name: the menu session carries settings
        # the CLI namespace has no place for (location groups, auto-snapshot)
        return run_menu(MenuStart(config_path=args.config,
                                  inventory_path=args.inventory,
                                  output_dir=args.output_dir, profile=args.profile,
                                  profiles_file=args.profiles_file))

    return _dispatch(args, console)


def _dispatch(args: argparse.Namespace, console: Console) -> int:
    """The non-interactive runs."""
    # the rack sheet is paperwork: a sheet in, a print version out - no config,
    # no credentials, no device
    if args.rack_sheet:
        return _run_rack_sheet(args, console)
    try:
        run = _prepare(args, console)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 2
    if args.dry_run:
        return _run_dry_run(args, run, console)
    # generating the MLT blocks is pure paperwork: sheet in, config out
    if args.generate_mlt:
        return _run_generate_mlt(args, console)
    return _run(args, run, console)


if __name__ == "__main__":
    raise SystemExit(main())
