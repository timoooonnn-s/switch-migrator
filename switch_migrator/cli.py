"""Command-line entry point: orchestrates collection, comparison, reporting."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import sys
from collections import Counter
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.console import Console

from switch_migrator import __version__
from switch_migrator.collectors.dvr import collect_dvr
from switch_migrator.collectors.switch import collect_switch
from switch_migrator.compare import compare_switch, resolve_binding_isids
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
    default_inventory,
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
from switch_migrator import cabling_sheet, handover, health, location
from switch_migrator import manifest as manifest_mod
from switch_migrator import mac_check, mlt_generate, snapshot as snapshot_mod, standard, verify
from switch_migrator.plan import Layout as PlanLayout, make_plan, pins_from_sheet
from switch_migrator.report import console as console_report
from switch_migrator.report.migration_tables import (
    build_health,
    build_health_summary,
    build_verification,
    build_verification_summary,
)
from switch_migrator.report.progress import NullProgress, make_progress
from switch_migrator.report.migration import (
    assign_port_uids,
    build_cabling,
    build_cabling_by_location,
    build_commands,
    build_plan_tables,
    build_port_info,
    new_switch_names,
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
                             "not exist yet. Needs migration-check installed.")
    parser.add_argument("--mac-baseline", type=Path, action="append",
                        default=[], metavar="DIR",
                        help="with --verify-migration: also check every "
                             "end-device MAC the old switches learned - back "
                             "anywhere, right VLAN, on the port the sheet "
                             "says? DIR is a --mac-snapshot folder, a "
                             "migration-check snapshot, or a --save-raw folder "
                             "of old ERS switches taken with --migration-sheets. "
                             "Repeatable: several baselines are merged. "
                             "Needs migration-check installed.")
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
                        default=Path("profiles.yaml"),
                        help="where the named profiles live "
                             "(default: profiles.yaml)")
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
                cfg: Config, args: argparse.Namespace,
                console: str = "") -> BaseRunner:
    if getattr(args, "dry_run", False):
        return DryRunRunner(name, platform,
                            command_overrides=cfg.command_overrides)
    if args.offline:
        return OfflineRunner(name, args.offline,
                             command_overrides=cfg.command_overrides)
    raw_dir = (args.output_dir / "raw" / name) if args.save_raw else None
    # the sheets read the running-config for its tagging and then drop the
    # text; capturing it to disk would keep exactly what that discards
    raw_skip = (() if getattr(args, "extract_config", False)
                else ("show running-config",))
    return SshRunner(name=name, host=host, platform=platform, creds=creds,
                     ssh=cfg.ssh, raw_dir=raw_dir, raw_skip=raw_skip,
                     command_overrides=cfg.command_overrides,
                     console=console, console_server=cfg.console_server)


def _needs_config(args: argparse.Namespace) -> bool:
    """Should this run pull `show running-config`?

    Beyond --extract-config, the migration sheets need it too: it is the only
    source that says whether a VLAN egresses a port tagged or untagged, which
    is exactly what gets configured on the new switch. One extra command per
    device buys the tagging column and authoritative VLAN membership.
    """
    return bool(getattr(args, "extract_config", False)
                or getattr(args, "migration_sheets", False))


def audit_one_switch(target: SwitchTarget, creds: Credentials, cfg: Config,
                     args: argparse.Namespace,
                     progress: NullProgress | None = None,
                     command_sink: dict[str, list[dict]] | None = None,
                     mac_recorder: mac_check.SnapshotRecorder | None = None
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
                                 creds, cfg, args,
                                 console=getattr(target, "console", ""))
        except ConnectionFailed as exc:
            failed.errors.append(str(exc))
            log.error("%s", exc)
            if mac_recorder is not None:
                mac_recorder.unreachable(target, str(exc))
            return failed
        except Exception as exc:  # noqa: BLE001 - one bad device must not kill the run
            failed.errors.append(
                f"unexpected connect error: {exc.__class__.__name__}: {exc}")
            log.exception("[%s] unexpected connect error", target.name)
            if mac_recorder is not None:
                mac_recorder.unreachable(target, str(exc))
            return failed
        runner.on_command = progress.device_command
        # with a MAC snapshot to record, the collectors talk to the switch
        # through a memo, so migration-check's commands that were already sent
        # are taken from there instead of being sent a second time
        memo = (mac_check.AnswerMemo(runner)
                if mac_recorder is not None and mac_recorder.wants(target.name)
                else None)
        try:
            audit = collect_switch(
                target, memo or runner, cfg,
                pull_config=_needs_config(args),
                keep_config=bool(getattr(args, "extract_config", False)),
                # verification needs the learned MACs on the NEW ports - they
                # are the evidence that the right cable went into the right hole
                pull_macs=bool(getattr(args, "migration_sheets", False)
                               or getattr(args, "verify_migration", None)))
            return audit
        except Exception as exc:  # noqa: BLE001 - same: isolate per-device failures
            failed.errors.append(
                f"collection crashed: {exc.__class__.__name__}: {exc}")
            log.exception("[%s] collection crashed", target.name)
            return failed
        finally:
            if memo is not None:
                _record_mac_snapshot(mac_recorder, target, runner, memo, audit)
            runner.close()
    finally:
        sent = list(getattr(runner, "command_log", []) or [])
        if command_sink is not None:
            command_sink[target.name] = [vars(r) for r in sent]
        progress.device_done(audit, len(sent))


def _record_mac_snapshot(recorder: mac_check.SnapshotRecorder, target: SwitchTarget,
                         runner: BaseRunner, memo: mac_check.AnswerMemo,
                         audit: SwitchAudit) -> None:
    """migration-check's commands on the session that is still open. Its failure
    is the MAC snapshot's problem, never the collection's."""
    try:
        recorder.record(target, runner, memo)
    except Exception as exc:  # noqa: BLE001 - the audit is already complete
        log.exception("[%s] MAC snapshot failed", target.name)
        audit.warnings.append(f"MAC snapshot (migration-check) failed: "
                              f"{exc.__class__.__name__}: {exc}")


def run_collection(targets: list[SwitchTarget], creds: Credentials, cfg: Config,
                   args: argparse.Namespace,
                   progress: NullProgress | None = None,
                   command_sink: dict[str, list[dict]] | None = None,
                   mac_recorder: mac_check.SnapshotRecorder | None = None
                   ) -> list[SwitchAudit]:
    """Collect every target in parallel, name-sorted. Shared by the flag
    interface and the interactive menu so both behave identically.

    With `mac_recorder`, migration-check's commands are sent on each switch's
    session as well and written to that snapshot; the caller finishes it."""
    audits: list[SwitchAudit] = []
    with cf.ThreadPoolExecutor(max_workers=cfg.ssh.workers) as pool:
        futures = [pool.submit(audit_one_switch, t, creds, cfg, args,
                               progress, command_sink, mac_recorder)
                   for t in targets]
        for future in cf.as_completed(futures):
            audits.append(future.result())
    audits.sort(key=lambda a: a.name)
    return audits


def preview_commands(targets: list[SwitchTarget], cfg: Config,
                     args: argparse.Namespace,
                     dvrs: list[DvrTarget] | None = None,
                     mac_for: Collection[str] = ()) -> dict[str, list[str]]:
    """--dry-run: what each device WOULD be asked, without connecting.

    The preview is produced by running the REAL collectors against a runner
    that connects to nothing and answers every command with an empty string.
    So the list cannot drift away from what the tool does, and because empty
    output sends the collectors down every fallback branch it is the full set
    of commands that could be sent - not just the subset one release accepts.

    `mac_for` names the switches a MAC snapshot would be recorded for; their
    list includes migration-check's commands, minus those already sent.
    """
    preview: dict[str, list[str]] = {}
    for target in targets:
        runner = DryRunRunner(target.name, target.platform,
                              command_overrides=cfg.command_overrides)
        memo = mac_check.AnswerMemo(runner) if target.name in mac_for else None
        try:
            collect_switch(target, memo or runner, cfg,
                           pull_config=_needs_config(args),
                           pull_macs=bool(getattr(args, "migration_sheets", False)
                                          or getattr(args, "verify_migration", None)))
            if memo is not None:
                mac_check.send_remaining(target.platform, runner, memo)
        except Exception:  # noqa: BLE001 - a preview must never fail the run
            log.exception("[%s] dry-run preview incomplete", target.name)
        preview[target.name] = runner.commands
    for dvr in (dvrs or []):
        runner = DryRunRunner(dvr.name, Platform.VOSS,
                              command_overrides=cfg.command_overrides)
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
                  f"'show', the 'enable' privilege mode switch, or a "
                  f"terminal-paging setting - the tool never configures "
                  f"anything. On releases that reject the bare "
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


def write_new_switch_configs(audits: list[SwitchAudit], plan, cfg: Config,
                             output_dir: Path, console: Console) -> list[Path]:
    """--plan: one config per new switch (+ an I-SID decision worksheet when a
    VLAN still needs one) into <output>/config/new/. Shared with the menu."""
    from switch_migrator.new_switch_config import render_all

    out_dir = output_dir / "config" / "new"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in render_all(audits, plan, cfg):
        path = out_dir / f"{result.switch}.cfg"
        path.write_text(result.text, encoding="utf-8")
        written.append(path)
        if result.decisions:
            sheet = out_dir / f"{result.switch}.isid-decisions.txt"
            sheet.write_text(build_worksheet(result.decisions,
                                             device_name=result.switch),
                             encoding="utf-8")
            written.append(sheet)
        flags = []
        if result.decisions:
            flags.append(f"{len(result.decisions)} I-SID decision(s) needed")
        if result.problems:
            flags.append(f"{len(result.problems)} problem(s)")
        if result.unverified:
            flags.append(f"{len(result.unverified)} unverified line kind(s)")
        console.print(f"Config for {result.switch}: {path}"
                      + (f" [yellow]({', '.join(flags)})[/yellow]" if flags else ""))
    return written


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
            path.write_text(text, encoding="utf-8")
            written.append(path)
        else:  # ERS -> VOSS flex-UNI (generated draft) + I-SID worksheet
            matched = {c.vlan_id: c.matched_isid
                       for c in comparisons.get(a.name, []) if c.matched_isid}
            model = parse_ers_config(a.running_config)
            res = generate_voss_from_ers(model, cfg, matched_by_vlan=matched,
                                         device_name=a.name)
            path = out_dir / f"{a.name}.cfg"
            path.write_text(res.text, encoding="utf-8")
            written.append(path)
            wpath = out_dir / f"{a.name}.isid-decisions.txt"
            wpath.write_text(build_worksheet(res.decisions, device_name=a.name),
                             encoding="utf-8")
            written.append(wpath)
    return written


def _load_sheet(path: Path, args: argparse.Namespace):
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
        raise ConfigError("--mac-baseline goes with --verify-migration")
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
    mac_check.require("--mac-snapshot" if args.mac_snapshot else "--mac-baseline")
    unreadable = mac_check.unreadable_baselines(list(args.mac_baseline))
    if unreadable and console is not None:
        console.print(
            f"[bold yellow]MAC baseline:[/bold yellow] {len(unreadable)} old VOSS "
            f"folder(s) hold only 'show interfaces gigabitEthernet fdb-entry', which "
            f"migration-check cannot read - their MACs are left out of the "
            f"comparison: {', '.join(unreadable)}. Take VOSS baselines with "
            f"--mac-snapshot.")


def _check_plan_args(args: argparse.Namespace, cfg: Config):
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
    pins = {}
    if args.pins:
        try:
            pins = pins_from_sheet(_load_sheet(args.pins, args).rows)
        except cabling_sheet.SheetError as exc:
            raise ConfigError(f"--pins: {exc}") from None
    return layout, pins


def _print_plan(plan, console: Console) -> None:
    for fill in plan.fill:
        console.print(f"Plan {fill.name}: {fill.dual} dual-homed + {fill.single} "
                      f"single-homed + {fill.pinned} pinned of {fill.access_ports} "
                      f"access ports, {fill.free} free")
    console.print(f"Plan: {len(plan.moves)} link(s) placed, "
                  f"{len(plan.not_moved)} not moved (see the 'Not moved' sheet)")
    for problem in plan.problems:
        console.print(f"  [bold yellow]![/bold yellow] {problem}")
    if plan.hints:
        console.print(f"  [dim]{len(plan.hints)} hint(s) on the 'Plan notes' sheet[/dim]")


def _mac_check_targets(targets: list[SwitchTarget],
                       sheet: cabling_sheet.Sheet) -> list[SwitchTarget]:
    """Only the switches links moved onto: an inventory given with -i may also
    name the old switches, which are the baseline, not the result."""
    new_switches = set(sheet.by_new_switch())
    return [t for t in targets if t.name in new_switches] or targets


def run_mac_check(baselines: list[Path], after: Path, sheet: cabling_sheet.Sheet,
                  output_dir: Path, stamp: str, *, watch, targets: list[SwitchTarget],
                  open_runner, workers: int, console: Console) -> mac_check.MacCheck:
    """The MAC comparison after a verification, with its console output. Shared by
    the flag interface and the menu."""
    if watch is not None:
        console.print(f"MAC check: watching the new switches every "
                      f"{watch.interval_seconds:g} s, at most {watch.max_rounds} "
                      f"round(s) [dim](Ctrl-C stops)[/dim]")
    check = mac_check.check(
        baselines, after, sheet, output_dir, stamp, watch=watch, targets=targets,
        open_runner=open_runner, workers=workers,
        on_round=lambda line: console.print(f"  {line.strip()}"))
    if check.stopped:
        console.print(f"  [dim]{check.stopped}[/dim]")
    color = "bold green" if check.settled else "bold yellow"
    console.print(f"[{color}]MAC check:[/{color}] {check.summary}")
    for _, device, problem in check.comparison.problems:
        console.print(f"  [yellow]{device}: {problem}[/yellow]")
    console.print(f"  [dim]details: {check.text}[/dim]")
    return check


def _mac_recorder(args: argparse.Namespace, targets: list[SwitchTarget],
                  stamp: str) -> mac_check.SnapshotRecorder | None:
    """The MAC snapshot this run records on its sessions, if any: the baseline
    (--mac-snapshot), or the after side of the MAC check (--mac-baseline)."""
    if args.mac_snapshot:
        return mac_check.SnapshotRecorder(args.mac_snapshot,
                                          label=args.mac_snapshot.name)
    if args.mac_baseline:
        sheet = _load_sheet(args.verify_migration, args)
        return mac_check.SnapshotRecorder(
            mac_check.after_snapshot_path(args.output_dir, stamp, args.mac_watch),
            label="after",
            only={t.name for t in _mac_check_targets(targets, sheet)})
    return None


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


def write_rack_sheet(sheet: cabling_sheet.Sheet, output_dir: Path) -> Path:
    """The rack sheet workbook, next to the other reports. Shared with the menu."""
    from switch_migrator.report.rack_sheet import build_rack_sheets

    path = output_dir / f"rack-sheet-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    write_excel(build_rack_sheets(sheet), path)
    return path


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
    from switch_migrator import profiles as profiles_mod

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
    if "output_dir" in prof and args.output_dir == Path("output"):
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


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    args = build_arg_parser().parse_args(argv)
    console = Console(stderr=True)

    # The profile can set output_dir, and the log file lives inside it - so it
    # has to be applied BEFORE logging is set up, or the reports go to the
    # profile's directory and their log to ./output.
    if args.profile:
        try:
            _apply_cli_profile(args, console)
        except ConfigError as exc:
            console.print(f"[bold red]Config error:[/bold red] {exc}")
            return 2
    setup_logging(args.output_dir, args.debug)

    # No arguments at all (or an explicit --menu): open the interactive toolkit
    # menu. Every flag keeps working exactly as before.
    if args.menu or not raw_argv:
        from switch_migrator.menu import run_menu
        # the profile is handed on by name: the menu session carries settings
        # the CLI namespace has no place for (location groups, auto-snapshot),
        # and applying it there sets all of them
        return run_menu(config_path=args.config, inventory_path=args.inventory,
                        output_dir=args.output_dir,
                        profile=args.profile,
                        profiles_file=args.profiles_file)

    # the rack sheet is paperwork: a sheet in, a print version out - no
    # config, no credentials, no device
    if args.rack_sheet:
        return _run_rack_sheet(args, console)

    # Reporting from a saved snapshot: no config, no credentials, no device is
    # touched - every sheet below is a pure function of the collected state.
    from_snapshot = getattr(args, "from_snapshot", None)
    try:
        # generating MLT blocks reads a sheet, not a fabric; a snapshot run
        # compares nothing. Neither needs the DvR/I-SID sections to be present.
        cfg = load_config(args.config,
                          require_fabric=(not args.no_fabric
                                          and not from_snapshot
                                          and not args.generate_mlt))
        if not args.inventory and not args.switch and not from_snapshot \
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
        for raw in args.switch:
            targets.append(parse_switch_arg(raw))
        if args.location_group:
            try:
                location.parse_group_args(args.location_group)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
        if args.verify_migration and not targets:
            # the sheet already names the switches to check: the new ones the
            # links were moved onto. They are fabric leaves, hence VOSS.
            targets = _targets_from_sheet(args, console)
        _check_mac_args(args, from_snapshot, console)
        plan_setup = _check_plan_args(args, cfg) if args.plan else None
        if not targets and not from_snapshot and not args.generate_mlt:
            raise ConfigError("no switches given: use -i inventory.yaml and/or "
                              "-s NAME:PLATFORM[:HOST]")
        counts = Counter(t.name for t in targets)
        dupes = {name for name, n in counts.items() if n > 1}
        if dupes:
            raise ConfigError(f"duplicate switch names: {', '.join(sorted(dupes))}")
        if ((args.split_by_location or args.location_group)
                and not args.migration_sheets):
            console.print("[yellow]--split-by-location/--location-group only "
                          "affect the cabling sheet - add --migration-sheets "
                          "for them to have any effect.[/yellow]")

        if (args.offline or args.dry_run or args.generate_mlt
                or from_snapshot):
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
        mac_for: set[str] = set()
        if args.mac_snapshot:
            mac_for = {t.name for t in targets}
        elif args.mac_baseline:
            mac_for = {t.name for t in _mac_check_targets(
                targets, _load_sheet(args.verify_migration, args))}
        render_dry_run(preview_commands(
            targets, cfg, args,
            dvrs=[] if args.no_fabric else cfg.dvr_controllers,
            mac_for=mac_for), console)
        return 0

    # generating the MLT blocks is pure paperwork: sheet in, config out
    if args.generate_mlt:
        return _run_generate_mlt(args, console)

    started = datetime.now()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    commands_by_device: dict[str, list[dict]] = {}
    try:
        mac_recorder = _mac_recorder(args, targets, stamp)
    except Exception as exc:  # noqa: BLE001 - checked above; this is the unexpected rest
        console.print(f"[bold red]MAC snapshot could not be started:[/bold red] {exc}")
        return 2

    def finish_mac_snapshot() -> None:
        if mac_recorder is None:
            return
        mac_recorder.finish()
        if args.mac_snapshot:
            console.print(f"MAC snapshot (migration-check baseline): "
                          f"[bold]{mac_recorder.path}[/bold]")

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
                                    progress, commands_by_device, mac_recorder)
        finish_mac_snapshot()
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
                                        progress, commands_by_device, mac_recorder)
                fabric = fabric_future.result()
        # before the DvR check below: a baseline taken of the switches is worth
        # keeping even when the fabric could not be read
        finish_mac_snapshot()
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
    # the comparison is the only thing that knows which I-SID a VLAN without a
    # local binding lands on - push that answer back into the port/MLT bindings
    # so the migration sheets can show it instead of an empty cell
    resolve_binding_isids(audits, comparisons,
                          fabric_checked=not args.no_fabric)

    # 4) Report
    tables = build_all(audits, fabric, comparisons, no_fabric=args.no_fabric)
    migration_plan = None
    if args.migration_sheets:
        assign_port_uids(audits)
        targets = new_switch_names(args.new_switch)
        if plan_setup is not None:
            layout, pins = plan_setup
            migration_plan = make_plan(audits, targets, layout,
                                       mlt_name=cfg.plan.mlt_name, pins=pins)
            _print_plan(migration_plan, console)
        tables += [build_port_info(audits),
                   build_cabling(audits, targets, migration_plan)]
        if migration_plan is not None:
            tables += build_plan_tables(migration_plan)

    if args.migration_sheets and (args.split_by_location or args.location_group):
        rules = cfg.locations
        if args.location_group:
            # a per-run scope replaces the config's groups, but keeps the
            # name patterns - those describe the estate, not this window
            rules = replace(rules, groups=location.parse_group_args(
                args.location_group))
        # the combined sheet is dropped, not kept alongside: this is a
        # fill-in document, and one link on two sheets means one set of
        # answers gets lost
        tables = [t for t in tables if t.title != "Cabling"]
        tables += build_cabling_by_location(
            audits, rules, new_switch_names(args.new_switch), migration_plan)
        console.print("Cabling sheet split by location:")
        for line in location.describe(rules, [a.name for a in audits]):
            console.print(f"  {line}")

    health_report = None
    if args.health_check:
        health_report = health.check(audits, comparisons, cfg)
        tables += [build_health_summary(health_report),
                   build_health(health_report)]

    verify_report = None
    if args.verify_migration:
        try:
            sheet = _load_sheet(args.verify_migration, args)
        except cabling_sheet.SheetError as exc:
            console.print(f"[bold red]Cabling sheet:[/bold red] {exc}")
            return 2
        verify_report = verify.verify(sheet, audits)
        extra = verify.unexpected_ports(sheet, audits)
        tables += [build_verification_summary(verify_report, extra),
                   build_verification(verify_report)]

    console_report.render(tables, Console(), verbose=args.verbose)
    if health_report is not None:
        _render_health(health_report, console)
    if verify_report is not None:
        counts = verify_report.counts()
        color = "bold red" if counts[verify.FAIL] else (
            "bold yellow" if counts[verify.WARN] else "bold green")
        console.print(f"\n[{color}]Migration verification:[/{color}] "
                      f"{counts[verify.PASS]} pass, {counts[verify.WARN]} warn, "
                      f"{counts[verify.FAIL]} fail, "
                      f"{counts[verify.PENDING]} not migrated yet")
        for problem in verify_report.problems:
            console.print(f"  [yellow]{problem}[/yellow]")
        for v in verify_report.verdicts:
            if v.result == verify.FAIL:
                console.print(f"  [red]FAIL[/red] {v.uid} {v.old_switch} "
                              f"{v.old_port} -> {v.new_switch} {v.new_port}: "
                              f"{v.why}")

    written: list[Path] = []
    mac_check_failed = False
    if args.mac_baseline and verify_report is not None and mac_recorder is not None:
        # later --mac-watch rounds re-read the new switches on fresh sessions;
        # without --save-raw, so a round never overwrites the run's raw capture
        round_args = argparse.Namespace(**{**vars(args), "save_raw": False})
        try:
            check = run_mac_check(
                args.mac_baseline, mac_recorder.path, sheet, args.output_dir, stamp,
                watch=cfg.mac_check if args.mac_watch else None,
                targets=_mac_check_targets(targets, sheet),
                open_runner=lambda t: make_runner(t.name, t.host, t.platform, creds,
                                                  cfg, round_args, console=t.console),
                workers=cfg.ssh.workers, console=console)
        except Exception as exc:  # noqa: BLE001 - the other reports must still be written
            mac_check_failed = True
            log.exception("MAC check failed")
            console.print(f"[bold red]MAC check could not run:[/bold red] {exc}")
        else:
            written += [check.text, check.json]
    if not args.no_excel:
        xlsx = args.output_dir / f"migration-audit-{stamp}.xlsx"
        write_excel(tables, xlsx)
        written.append(xlsx)
    if args.csv:
        written.extend(write_csv(tables, args.output_dir / f"csv-{stamp}"))
    if args.extract_config:
        written.extend(_write_config_extracts(
            audits, args.output_dir, comparisons, cfg, console))
    if migration_plan is not None:
        written.extend(write_new_switch_configs(audits, migration_plan, cfg,
                                                args.output_dir, console))
    if args.migration_sheets:
        cmd_path = args.output_dir / f"migration-commands-{stamp}.txt"
        cmd_path.write_text(build_commands(audits, new_switch=args.new_switch),
                            encoding="utf-8")
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
    # a health BLOCK or a failed link is exactly the case a script must catch,
    # even when the audit itself found nothing else wrong
    if health_report is not None and health_report.verdict == health.BLOCK:
        code = 1
    if verify_report is not None and not verify_report.ok:
        code = 1
    # a MAC not back yet is a warning, like 'no MAC yet' in the verification; a
    # check that could not run at all is an error a script must see
    if mac_check_failed:
        code = 1

    if args.manifest:
        path = manifest_mod.write(
            args.output_dir / f"manifest-{stamp}.json",
            manifest_mod.build(audits, fabric, args, started, datetime.now(),
                               written, commands_by_device,
                               config_path=args.config, exit_code=code))
        console.print(f"Run manifest: [bold]{path}[/bold]")
        written.append(path)

    # last, so the folder contains everything the run produced - the manifest
    # included, which is the file a change record actually wants
    if args.handover:
        bundle = handover.build(
            args.output_dir, written, tables, audits,
            meta={"started": started.isoformat(timespec="seconds"),
                  "config": str(args.config),
                  "switches": ", ".join(a.name for a in audits)},
            stamp=stamp)
        console.print(f"Handover bundle: [bold]{bundle}[/bold]\n"
                      f"  open [bold]{bundle / 'index.html'}[/bold]")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
