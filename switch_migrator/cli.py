"""Command-line entry point: orchestrates collection, comparison, reporting."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.console import Console

from switch_migrator import __version__
from switch_migrator.collectors.dvr import collect_dvr
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch
from switch_migrator.config_extract import extract_voss_config
from switch_migrator.config_generate import generate_voss_from_ers
from switch_migrator.isid import build_worksheet
from switch_migrator.parsers.ers_config import parse_ers_config
from switch_migrator.config import (
    DvrTarget,
    Config,
    ConfigError,
    Credentials,
    SwitchTarget,
    get_credentials,
    load_config,
    load_inventory,
    parse_switch_arg,
)
from switch_migrator.connection import (
    BaseRunner,
    ConnectionFailed,
    DryRunRunner,
    OfflineRunner,
    SshRunner,
    enable_legacy_ssh_algorithms,
)
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator import manifest as manifest_mod
from switch_migrator import snapshot as snapshot_mod
from switch_migrator.report import console as console_report
from switch_migrator.report.progress import NullProgress, make_progress
from switch_migrator.report.migration import (
    assign_port_uids,
    build_cabling,
    build_commands,
    build_port_info,
)
from switch_migrator.report.excel import write_csv, write_excel
from switch_migrator.report.tables import build_all

log = logging.getLogger("switch_migrator")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switch-migrator",
        description="Pre-migration audit: collects port/MLT/IST state from "
                    "legacy Extreme VOSS/ERS switches and compares their VLANs "
                    "against the I-SID state on the DvR controllers. "
                    "Read-only: only 'show' commands are ever sent.",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config.yaml"),
                        help="config file (default: config.yaml)")
    parser.add_argument("-i", "--inventory", type=Path,
                        help="inventory YAML with the switches to migrate")
    parser.add_argument("-s", "--switch", action="append", default=[],
                        metavar="NAME:PLATFORM[:HOST]",
                        help="add a switch on the CLI, e.g. old-sw-01:ers "
                             "(repeatable, combines with -i)")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"),
                        help="directory for reports and logs (default: ./output)")
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
    parser.add_argument("--new-switch", metavar="NAME", default="",
                        help="name of the target switch, used in the "
                             "--migration-sheets command file")
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
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print the ports/MLTs/fabric tables to the console")
    parser.add_argument("--debug", action="store_true",
                        help="debug logging (includes netmiko)")
    parser.add_argument("--menu", action="store_true",
                        help="open the interactive toolkit menu (also the "
                             "default when no arguments are given): select "
                             "switches, collect once, then produce any output "
                             "from that same data")
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


def make_runner(name: str, host: str, platform: Platform, creds: Credentials,
                cfg: Config, args: argparse.Namespace) -> BaseRunner:
    if getattr(args, "dry_run", False):
        return DryRunRunner(name, platform)
    if args.offline:
        return OfflineRunner(name, args.offline)
    raw_dir = (args.output_dir / "raw" / name) if args.save_raw else None
    return SshRunner(name=name, host=host, platform=platform, creds=creds,
                     ssh=cfg.ssh, raw_dir=raw_dir)


def audit_one_switch(target: SwitchTarget, creds: Credentials, cfg: Config,
                     args: argparse.Namespace,
                     progress: NullProgress | None = None,
                     command_sink: dict[str, list[dict]] | None = None
                     ) -> SwitchAudit:
    progress = progress or NullProgress()
    failed = SwitchAudit(name=target.name, host=target.host,
                         platform=target.platform, reachable=False)
    progress.device_start(target.name)
    runner: BaseRunner | None = None
    audit = failed
    try:
        try:
            runner = make_runner(target.name, target.host, target.platform,
                                 creds, cfg, args)
        except ConnectionFailed as exc:
            failed.errors.append(str(exc))
            log.error("%s", exc)
            return failed
        except Exception as exc:  # noqa: BLE001 - one bad device must not kill the run
            failed.errors.append(
                f"unexpected connect error: {exc.__class__.__name__}: {exc}")
            log.exception("[%s] unexpected connect error", target.name)
            return failed
        runner.on_command = progress.device_command
        try:
            audit = collect_switch(
                target, runner, cfg,
                pull_config=args.extract_config,
                pull_macs=getattr(args, "migration_sheets", False))
            return audit
        except Exception as exc:  # noqa: BLE001 - same: isolate per-device failures
            failed.errors.append(
                f"collection crashed: {exc.__class__.__name__}: {exc}")
            log.exception("[%s] collection crashed", target.name)
            return failed
        finally:
            runner.close()
    finally:
        sent = list(getattr(runner, "command_log", []) or [])
        if command_sink is not None:
            command_sink[target.name] = [vars(r) for r in sent]
        progress.device_done(audit, len(sent))


def run_collection(targets: list[SwitchTarget], creds: Credentials, cfg: Config,
                   args: argparse.Namespace,
                   progress: NullProgress | None = None,
                   command_sink: dict[str, list[dict]] | None = None
                   ) -> list[SwitchAudit]:
    """Collect every target in parallel, name-sorted. Shared by the flag
    interface and the interactive menu so both behave identically."""
    audits: list[SwitchAudit] = []
    with cf.ThreadPoolExecutor(max_workers=cfg.ssh.workers) as pool:
        futures = [pool.submit(audit_one_switch, t, creds, cfg, args,
                               progress, command_sink)
                   for t in targets]
        for future in cf.as_completed(futures):
            audits.append(future.result())
    audits.sort(key=lambda a: a.name)
    return audits


def preview_commands(targets: list[SwitchTarget], cfg: Config,
                     args: argparse.Namespace,
                     dvrs: list[DvrTarget] | None = None) -> dict[str, list[str]]:
    """--dry-run: what each device WOULD be asked, without connecting.

    The preview is produced by running the REAL collectors against a runner
    that connects to nothing and answers every command with an empty string.
    So the list cannot drift away from what the tool does, and because empty
    output sends the collectors down every fallback branch it is the full set
    of commands that could be sent - not just the subset one release accepts.
    """
    preview: dict[str, list[str]] = {}
    for target in targets:
        runner = DryRunRunner(target.name, target.platform)
        try:
            collect_switch(target, runner, cfg,
                           pull_config=args.extract_config,
                           pull_macs=getattr(args, "migration_sheets", False))
        except Exception:  # noqa: BLE001 - a preview must never fail the run
            log.exception("[%s] dry-run preview incomplete", target.name)
        preview[target.name] = runner.commands
    for dvr in (dvrs or []):
        runner = DryRunRunner(dvr.name, Platform.VOSS)
        try:
            collect_dvr(dvr.name, runner, FabricState())
        except Exception:  # noqa: BLE001 - same
            log.exception("[%s] dry-run preview incomplete", dvr.name)
        preview[f"{dvr.name} (DvR controller)"] = runner.commands
    return preview


def render_dry_run(preview: dict[str, list[str]], console: Console) -> None:
    console.print("[bold]Dry run[/bold] - nothing was connected to and no "
                  "command was sent.\n")
    total = 0
    for name, commands in preview.items():
        total += len(commands)
        console.print(f"[bold cyan]{name}[/bold cyan] "
                      f"({len(commands)} command(s))")
        for command in commands:
            console.print(f"    {command}")
        console.print("")
    console.print(f"[dim]{total} command(s) in total. Every one of them is a "
                  f"'show' or a terminal-paging setting - the tool never "
                  f"configures anything. On releases that reject the bare "
                  f"'show interfaces gigabitEthernet fdb-entry', each "
                  f"operationally up port adds one more of that same read-only "
                  f"command.[/dim]")


def collect_fabric(dvrs: list[DvrTarget], creds: Credentials, cfg: Config,
                   args: argparse.Namespace,
                   progress: NullProgress | None = None) -> FabricState:
    # fabric-wide listings (isis spbm i-sid) can be large - give the DvR
    # sessions extra read-timeout headroom
    dvr_cfg = replace(cfg, ssh=replace(cfg.ssh,
                                       read_timeout=max(cfg.ssh.read_timeout, 120)))

    tracker = progress or NullProgress()

    def collect_one(dvr: DvrTarget) -> FabricState:
        fragment = FabricState()
        try:
            runner = make_runner(dvr.name, dvr.host, Platform.VOSS,
                                 creds, dvr_cfg, args)
        except Exception as exc:  # noqa: BLE001 - keep reading the other DvRs
            fragment.dvr_errors.append(f"{dvr.name}: {exc}")
            log.error("%s: %s", dvr.name, exc)
            tracker.fabric_done(dvr.name, False)
            return fragment
        try:
            collect_dvr(dvr.name, runner, fragment)
        except Exception as exc:  # noqa: BLE001 - keep reading the other DvRs
            fragment.dvr_errors.append(f"{dvr.name}: collection crashed: {exc}")
            log.exception("[%s] collection crashed", dvr.name)
        finally:
            runner.close()
            tracker.fabric_done(dvr.name, dvr.name in fragment.dvrs_ok)
        return fragment

    fabric = FabricState()
    with cf.ThreadPoolExecutor(max_workers=min(len(dvrs), cfg.ssh.workers)) as pool:
        for fragment in pool.map(collect_one, dvrs):
            fabric.merge(fragment)
    return fabric


def _write_config_extracts(audits: list[SwitchAudit], output_dir: Path,
                           comparisons: dict, cfg: Config,
                           console: Console) -> list[Path]:
    """Write a migration-ready config per switch (--extract-config): VOSS boxes
    are filtered/neutralized; ERS boxes are translated to VOSS flex-UNI with an
    I-SID decision worksheet."""
    written: list[Path] = []
    out_dir = output_dir / "config"
    for a in audits:
        if not a.running_config:
            if a.reachable:
                console.print(f"[yellow]--extract-config: no running-config "
                              f"obtained from {a.name}[/yellow]")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        if a.platform is Platform.VOSS:
            text = extract_voss_config(a.running_config, device_name=a.name).text
            path = out_dir / f"{a.name}.cfg"
            path.write_text(text)
            written.append(path)
        else:  # ERS -> VOSS flex-UNI (generated draft) + I-SID worksheet
            matched = {c.vlan_id: c.matched_isid
                       for c in comparisons.get(a.name, []) if c.matched_isid}
            model = parse_ers_config(a.running_config)
            res = generate_voss_from_ers(model, cfg, matched_by_vlan=matched,
                                         device_name=a.name)
            path = out_dir / f"{a.name}.cfg"
            path.write_text(res.text)
            written.append(path)
            wpath = out_dir / f"{a.name}.isid-decisions.txt"
            wpath.write_text(build_worksheet(res.decisions, device_name=a.name))
            written.append(wpath)
    return written


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    args = build_arg_parser().parse_args(argv)
    console = Console(stderr=True)
    setup_logging(args.output_dir, args.debug)

    # No arguments at all (or an explicit --menu): open the interactive toolkit
    # menu. Every flag keeps working exactly as before.
    if args.menu or not raw_argv:
        from switch_migrator.menu import run_menu
        return run_menu(config_path=args.config, inventory_path=args.inventory,
                        output_dir=args.output_dir)

    # Reporting from a saved snapshot: no config, no credentials, no device is
    # touched - every sheet below is a pure function of the collected state.
    from_snapshot = getattr(args, "from_snapshot", None)
    try:
        cfg = load_config(args.config,
                          require_fabric=not args.no_fabric and not from_snapshot)
        targets: list[SwitchTarget] = []
        if args.inventory:
            targets.extend(load_inventory(args.inventory))
        for raw in args.switch:
            targets.append(parse_switch_arg(raw))
        if not targets and not from_snapshot:
            raise ConfigError("no switches given: use -i inventory.yaml and/or "
                              "-s NAME:PLATFORM[:HOST]")
        dupes = {t.name for t in targets if [x.name for x in targets].count(t.name) > 1}
        if dupes:
            raise ConfigError(f"duplicate switch names: {', '.join(sorted(dupes))}")

        if args.offline or args.dry_run or from_snapshot:
            creds = dvr_creds = Credentials("offline", "offline")
        else:
            creds = get_credentials("switches", "SM")
            # no-fabric mode never touches a DvR, so don't prompt for its creds
            dvr_creds = creds if args.no_fabric else get_credentials(
                "DvR controllers", "SM_DVR", fallback=creds)
            if cfg.ssh.legacy_algorithms:
                # initialize once on the main thread before any worker connects
                enable_legacy_ssh_algorithms()
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 2

    if args.dry_run:
        render_dry_run(preview_commands(
            targets, cfg, args,
            dvrs=[] if args.no_fabric else cfg.dvr_controllers), console)
        return 0

    started = datetime.now()
    commands_by_device: dict[str, list[dict]] = {}

    if from_snapshot:
        try:
            audits, fabric, snap_meta = snapshot_mod.load(from_snapshot)
        except snapshot_mod.SnapshotError as exc:
            console.print(f"[bold red]Snapshot error:[/bold red] {exc}")
            return 2
        console.print(f"[bold]switch-migrator {__version__}[/bold] - "
                      f"{snapshot_mod.describe(snap_meta, audits)}")
        console.print("[yellow]No device was contacted: reporting from the "
                      "saved snapshot.[/yellow]")
    elif args.no_fabric:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"inventory of {len(targets)} switch(es) "
                      f"[yellow](--no-fabric: no DvR comparison)[/yellow]")
        fabric = FabricState()
        with make_progress(console, len(targets)) as progress:
            audits = run_collection(targets, creds, cfg, args,
                                    progress, commands_by_device)
    else:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"audit of {len(targets)} switch(es) against "
                      f"{len(cfg.dvr_controllers)} DvR controller(s)")
        # The fabric read and the switch reads are independent - the comparison
        # is what needs both - so they run at the same time and the fabric
        # round-trip leaves the critical path.
        with make_progress(console, len(targets),
                           len(cfg.dvr_controllers)) as progress:
            with cf.ThreadPoolExecutor(max_workers=1) as fabric_pool:
                fabric_future = fabric_pool.submit(
                    collect_fabric, cfg.dvr_controllers, dvr_creds, cfg, args,
                    progress)
                audits = run_collection(targets, creds, cfg, args,
                                        progress, commands_by_device)
                fabric = fabric_future.result()
        if not fabric.dvrs_ok:
            console.print("[bold red]No DvR controller could be read - aborting, "
                          "there is no fabric state to compare against.[/bold red]")
            for err in fabric.dvr_errors:
                console.print(f"  [red]{err}[/red]")
            console.print("[dim]The switches were read successfully; re-run "
                          "with --no-fabric for an inventory report without "
                          "the fabric comparison.[/dim]")
            return 1
        console.print(f"Fabric state: {len(fabric.isids)} I-SIDs from "
                      f"{', '.join(fabric.dvrs_ok)}"
                      + (f" [yellow]({len(fabric.dvr_errors)} DvR error(s))[/yellow]"
                         if fabric.dvr_errors else ""))

    # unreachable devices go to the report too, but say it loudly right away
    for audit in audits:
        if not audit.reachable:
            reason = audit.errors[-1] if audit.errors else "unknown error"
            console.print(f"[bold red]UNREACHABLE[/bold red] {audit.name} "
                          f"({audit.host}): {reason}")

    # 3) Compare (skipped entirely in no-fabric mode)
    comparisons = {} if args.no_fabric else {
        a.name: compare_switch(a, fabric, cfg) for a in audits if a.reachable}

    # 4) Report
    tables = build_all(audits, fabric, comparisons, no_fabric=args.no_fabric)
    if args.migration_sheets:
        assign_port_uids(audits)
        tables += [build_port_info(audits), build_cabling(audits)]
    console_report.render(tables, Console(), verbose=args.verbose)

    written: list[Path] = []
    stamp = started.strftime("%Y%m%d-%H%M%S")
    if not args.no_excel:
        xlsx = args.output_dir / f"migration-audit-{stamp}.xlsx"
        write_excel(tables, xlsx)
        written.append(xlsx)
    if args.csv:
        written.extend(write_csv(tables, args.output_dir / f"csv-{stamp}"))
    if args.extract_config:
        written.extend(_write_config_extracts(
            audits, args.output_dir, comparisons, cfg, console))
    if args.migration_sheets:
        cmd_path = args.output_dir / f"migration-commands-{stamp}.txt"
        cmd_path.write_text(build_commands(audits, new_switch=args.new_switch))
        written.append(cmd_path)
    if args.save_snapshot is not None and not from_snapshot:
        # '--save-snapshot' on its own carries the empty sentinel -> default
        # name in the output dir; '--save-snapshot FILE' names it explicitly
        args.save_snapshot = (Path(args.save_snapshot) if args.save_snapshot
                              else args.output_dir / f"snapshot-{stamp}.json")
        written.append(snapshot_mod.save(
            args.save_snapshot, audits, fabric,
            meta={"started": started.isoformat(timespec="seconds"),
                  "config": str(args.config),
                  "no_fabric": bool(args.no_fabric)}))
    for path in written:
        console.print(f"Report written: [bold]{path}[/bold]")

    # Exit code mirrors the worst finding so the tool is scriptable
    severities = [t for table in tables for t in table.severities]
    unreachable = [a for a in audits if not a.reachable]
    code = 1 if ("error" in severities or unreachable or fabric.dvr_errors) else 0

    if args.manifest:
        path = manifest_mod.write(
            args.output_dir / f"manifest-{stamp}.json",
            manifest_mod.build(audits, fabric, args, started, datetime.now(),
                               written, commands_by_device,
                               config_path=args.config, exit_code=code))
        console.print(f"Run manifest: [bold]{path}[/bold]")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
