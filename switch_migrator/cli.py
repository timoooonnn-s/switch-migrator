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
    OfflineRunner,
    SshRunner,
    enable_legacy_ssh_algorithms,
)
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator.report import console as console_report
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
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print the ports/MLTs/fabric tables to the console")
    parser.add_argument("--debug", action="store_true",
                        help="debug logging (includes netmiko)")
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
    if args.offline:
        return OfflineRunner(name, args.offline)
    raw_dir = (args.output_dir / "raw" / name) if args.save_raw else None
    return SshRunner(name=name, host=host, platform=platform, creds=creds,
                     ssh=cfg.ssh, raw_dir=raw_dir)


def audit_one_switch(target: SwitchTarget, creds: Credentials, cfg: Config,
                     args: argparse.Namespace) -> SwitchAudit:
    failed = SwitchAudit(name=target.name, host=target.host,
                         platform=target.platform, reachable=False)
    try:
        runner = make_runner(target.name, target.host, target.platform,
                             creds, cfg, args)
    except ConnectionFailed as exc:
        failed.errors.append(str(exc))
        log.error("%s", exc)
        return failed
    except Exception as exc:  # noqa: BLE001 - one bad device must not kill the run
        failed.errors.append(f"unexpected connect error: {exc.__class__.__name__}: {exc}")
        log.exception("[%s] unexpected connect error", target.name)
        return failed
    try:
        return collect_switch(target, runner, cfg,
                              pull_config=args.extract_config)
    except Exception as exc:  # noqa: BLE001 - same: isolate per-device failures
        failed.errors.append(f"collection crashed: {exc.__class__.__name__}: {exc}")
        log.exception("[%s] collection crashed", target.name)
        return failed
    finally:
        runner.close()


def collect_fabric(dvrs: list[DvrTarget], creds: Credentials, cfg: Config,
                   args: argparse.Namespace) -> FabricState:
    # fabric-wide listings (isis spbm i-sid) can be large - give the DvR
    # sessions extra read-timeout headroom
    dvr_cfg = replace(cfg, ssh=replace(cfg.ssh,
                                       read_timeout=max(cfg.ssh.read_timeout, 120)))

    def collect_one(dvr: DvrTarget) -> FabricState:
        fragment = FabricState()
        try:
            runner = make_runner(dvr.name, dvr.host, Platform.VOSS,
                                 creds, dvr_cfg, args)
        except Exception as exc:  # noqa: BLE001 - keep reading the other DvRs
            fragment.dvr_errors.append(f"{dvr.name}: {exc}")
            log.error("%s: %s", dvr.name, exc)
            return fragment
        try:
            collect_dvr(dvr.name, runner, fragment)
        except Exception as exc:  # noqa: BLE001 - keep reading the other DvRs
            fragment.dvr_errors.append(f"{dvr.name}: collection crashed: {exc}")
            log.exception("[%s] collection crashed", dvr.name)
        finally:
            runner.close()
        return fragment

    fabric = FabricState()
    with cf.ThreadPoolExecutor(max_workers=min(len(dvrs), cfg.ssh.workers)) as pool:
        for fragment in pool.map(collect_one, dvrs):
            fabric.merge(fragment)
    return fabric


def _write_config_extracts(audits: list[SwitchAudit], output_dir: Path,
                           console: Console) -> list[Path]:
    """Write a neutralized VOSS config extract per switch (--extract-config)."""
    written: list[Path] = []
    out_dir = output_dir / "config"
    for a in audits:
        if a.platform is not Platform.VOSS:
            if a.reachable:
                console.print(f"[yellow]--extract-config: {a.name} is ERS - "
                              f"config extraction is VOSS-only for now[/yellow]")
            continue
        if not a.running_config:
            if a.reachable:
                console.print(f"[yellow]--extract-config: no running-config "
                              f"obtained from {a.name}[/yellow]")
            continue
        result = extract_voss_config(a.running_config, device_name=a.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{a.name}.cfg"
        path.write_text(result.text)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    console = Console(stderr=True)
    setup_logging(args.output_dir, args.debug)

    try:
        cfg = load_config(args.config, require_fabric=not args.no_fabric)
        targets: list[SwitchTarget] = []
        if args.inventory:
            targets.extend(load_inventory(args.inventory))
        for raw in args.switch:
            targets.append(parse_switch_arg(raw))
        if not targets:
            raise ConfigError("no switches given: use -i inventory.yaml and/or "
                              "-s NAME:PLATFORM[:HOST]")
        dupes = {t.name for t in targets if [x.name for x in targets].count(t.name) > 1}
        if dupes:
            raise ConfigError(f"duplicate switch names: {', '.join(sorted(dupes))}")

        if args.offline:
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

    started = datetime.now()
    if args.no_fabric:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"inventory of {len(targets)} switch(es) "
                      f"[yellow](--no-fabric: no DvR comparison)[/yellow]")
        fabric = FabricState()
    else:
        console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only "
                      f"audit of {len(targets)} switch(es) against "
                      f"{len(cfg.dvr_controllers)} DvR controller(s)")
        # 1) Fabric state from the DvR controllers (sequential merge)
        with console.status("Collecting fabric state from DvR controllers..."):
            fabric = collect_fabric(cfg.dvr_controllers, dvr_creds, cfg, args)
        if not fabric.dvrs_ok:
            console.print("[bold red]No DvR controller could be read - aborting, "
                          "there is no fabric state to compare against.[/bold red]")
            for err in fabric.dvr_errors:
                console.print(f"  [red]{err}[/red]")
            return 1
        console.print(f"Fabric state: {len(fabric.isids)} I-SIDs from "
                      f"{', '.join(fabric.dvrs_ok)}"
                      + (f" [yellow]({len(fabric.dvr_errors)} DvR error(s))[/yellow]"
                         if fabric.dvr_errors else ""))

    # 2) Legacy switches in parallel
    audits: list[SwitchAudit] = []
    with console.status(f"Auditing {len(targets)} switch(es)..."):
        with cf.ThreadPoolExecutor(max_workers=cfg.ssh.workers) as pool:
            futures = {pool.submit(audit_one_switch, t, creds, cfg, args): t
                       for t in targets}
            for future in cf.as_completed(futures):
                audits.append(future.result())
    audits.sort(key=lambda a: a.name)

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
        written.extend(_write_config_extracts(audits, args.output_dir, console))
    for path in written:
        console.print(f"Report written: [bold]{path}[/bold]")

    # Exit code mirrors the worst finding so the tool is scriptable
    severities = [t for table in tables for t in table.severities]
    unreachable = [a for a in audits if not a.reachable]
    if "error" in severities or unreachable or fabric.dvr_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
