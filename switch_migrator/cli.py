"""Command-line entry point: orchestrates collection, comparison, reporting."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

from switch_migrator import __version__
from switch_migrator.collectors.bcb import collect_bcb
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch
from switch_migrator.config import (
    BcbTarget,
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
                    "against the I-SID state on the BCB controllers. "
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
    try:
        runner = make_runner(target.name, target.host, target.platform,
                             creds, cfg, args)
    except ConnectionFailed as exc:
        audit = SwitchAudit(name=target.name, host=target.host,
                            platform=target.platform, reachable=False)
        audit.errors.append(str(exc))
        log.error("%s", exc)
        return audit
    try:
        return collect_switch(target, runner, cfg)
    finally:
        runner.close()


def collect_fabric(bcbs: list[BcbTarget], creds: Credentials, cfg: Config,
                   args: argparse.Namespace) -> FabricState:
    fabric = FabricState()
    for bcb in bcbs:
        try:
            runner = make_runner(bcb.name, bcb.host, Platform.VOSS,
                                 creds, cfg, args)
        except ConnectionFailed as exc:
            fabric.bcb_errors.append(str(exc))
            log.error("%s", exc)
            continue
        try:
            collect_bcb(bcb.name, runner, fabric)
        finally:
            runner.close()
    return fabric


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    console = Console(stderr=True)
    setup_logging(args.output_dir, args.debug)

    try:
        cfg = load_config(args.config)
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
            creds = bcb_creds = Credentials("offline", "offline")
        else:
            creds = get_credentials("switches", "SM")
            bcb_creds = get_credentials("BCB controllers", "SM_BCB", fallback=creds)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        return 2

    started = datetime.now()
    console.print(f"[bold]switch-migrator {__version__}[/bold] - read-only audit "
                  f"of {len(targets)} switch(es) against "
                  f"{len(cfg.bcb_controllers)} BCB(s)")

    # 1) Fabric state from the BCBs (sequential merge, small device count)
    with console.status("Collecting fabric state from BCB controllers..."):
        fabric = collect_fabric(cfg.bcb_controllers, bcb_creds, cfg, args)
    if not fabric.bcbs_ok:
        console.print("[bold red]No BCB controller could be read - aborting, "
                      "there is no fabric state to compare against.[/bold red]")
        for err in fabric.bcb_errors:
            console.print(f"  [red]{err}[/red]")
        return 1
    console.print(f"Fabric state: {len(fabric.isids)} I-SIDs from "
                  f"{', '.join(fabric.bcbs_ok)}"
                  + (f" [yellow]({len(fabric.bcb_errors)} BCB error(s))[/yellow]"
                     if fabric.bcb_errors else ""))

    # 2) Legacy switches in parallel
    audits: list[SwitchAudit] = []
    with console.status(f"Auditing {len(targets)} switch(es)..."):
        with cf.ThreadPoolExecutor(max_workers=cfg.ssh.workers) as pool:
            futures = {pool.submit(audit_one_switch, t, creds, cfg, args): t
                       for t in targets}
            for future in cf.as_completed(futures):
                audits.append(future.result())
    audits.sort(key=lambda a: a.name)

    # 3) Compare
    comparisons = {a.name: compare_switch(a, fabric, cfg)
                   for a in audits if a.reachable}

    # 4) Report
    tables = build_all(audits, fabric, comparisons)
    console_report.render(tables, Console(), verbose=args.verbose)

    written: list[Path] = []
    stamp = started.strftime("%Y%m%d-%H%M%S")
    if not args.no_excel:
        xlsx = args.output_dir / f"migration-audit-{stamp}.xlsx"
        write_excel(tables, xlsx)
        written.append(xlsx)
    if args.csv:
        written.extend(write_csv(tables, args.output_dir / f"csv-{stamp}"))
    for path in written:
        console.print(f"Report written: [bold]{path}[/bold]")

    # Exit code mirrors the worst finding so the tool is scriptable
    severities = [t for table in tables for t in table.severities]
    unreachable = [a for a in audits if not a.reachable]
    if "error" in severities or unreachable or fabric.bcb_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
