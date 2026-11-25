"""Collect: read the switches and the fabric, or replay or preview that read.

Every device is reached through one runner - live SSH (legacy algorithms, console
server), offline replay, or the dry run that only records - so the same code path
serves all three.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.console import Console

from switch_migrator import mac_check
from switch_migrator.collectors.dvr import collect_dvr
from switch_migrator.collectors.switch import Reads, collect_switch
from switch_migrator.config import Config, Credentials, DvrTarget, SwitchTarget
from switch_migrator.connection import (
    BaseRunner,
    CommandEntry,
    ConnectionFailed,
    Device,
    DryRunRunner,
    OfflineRunner,
    RunnerSettings,
    SshRunner,
)
from switch_migrator.models import FabricState, Platform, SwitchAudit
from switch_migrator.report.progress import NullProgress

log = logging.getLogger("switch_migrator")


@dataclass(frozen=True)
class CollectOptions:
    """What one collection reads, and how it reaches the devices."""
    reads: Reads = field(default_factory=Reads.for_run)   # what each switch read includes
    fabric: bool = False          # also read the DvR controllers
    offline: Path | None = None   # replay this --save-raw capture instead of SSH
    raw_dir: Path | None = None   # write every answer under <raw_dir>/<device>/
    dry_run: bool = False         # record the commands, connect to nothing


@dataclass
class CollectContext:
    """One collection run: its settings, and what it keeps track of as it goes."""
    cfg: Config
    opts: CollectOptions
    progress: NullProgress = field(default_factory=NullProgress)
    # migration-check's commands, recorded on the same sessions (the caller's
    # snapshot; collect() finishes it)
    mac_recorder: mac_check.SnapshotRecorder | None = None
    # every command sent, per device (the manifest's input)
    commands: dict[str, list[CommandEntry]] = field(default_factory=dict)


@dataclass
class Collected:
    audits: list[SwitchAudit]
    fabric: FabricState = field(default_factory=FabricState)
    commands: dict[str, list[CommandEntry]] = field(default_factory=dict)   # for the manifest


def make_runner(target: SwitchTarget | DvrTarget, platform: Platform,
                creds: Credentials, cfg: Config, opts: CollectOptions) -> BaseRunner:
    if opts.dry_run:
        return DryRunRunner(target.name, platform,
                            command_overrides=cfg.command_overrides)
    if opts.offline:
        return OfflineRunner(target.name, opts.offline,
                             command_overrides=cfg.command_overrides)
    # the sheets read the running-config for its tagging and then drop the
    # text; capturing it to disk would keep exactly what that discards
    raw_skip = () if opts.reads.keep_config else ("show running-config",)
    device = Device(target.name, target.host, platform,
                    console=getattr(target, "console", ""))
    return SshRunner(device, creds, RunnerSettings(
        cfg.ssh, command_overrides=cfg.command_overrides,
        console_server=cfg.console_server,
        raw_dir=(opts.raw_dir / target.name) if opts.raw_dir else None,
        raw_skip=raw_skip))


def collect_one_switch(target: SwitchTarget, creds: Credentials,
                       ctx: CollectContext) -> SwitchAudit:
    """One switch, isolated: whatever goes wrong becomes an error on its audit,
    never an exception that costs the other switches their data."""
    cfg, opts, progress, mac_recorder = ctx.cfg, ctx.opts, ctx.progress, ctx.mac_recorder
    failed = SwitchAudit(name=target.name, host=target.host,
                         platform=target.platform, reachable=False)
    progress.device_start(target.name)
    runner: BaseRunner | None = None
    audit = failed
    try:
        try:
            runner = make_runner(target, target.platform, creds, cfg, opts)
        except ConnectionFailed as exc:
            failed.errors.append(str(exc))
            log.error("%s", exc)
            if mac_recorder is not None:
                mac_recorder.unreachable(target, str(exc))
            return failed
        except Exception as exc:  # broad on purpose: one bad device must not kill the run
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
            audit = collect_switch(target, memo or runner, cfg, opts.reads)
            return audit
        except Exception as exc:  # broad on purpose: same: isolate per-device failures
            failed.errors.append(
                f"collection crashed: {exc.__class__.__name__}: {exc}")
            log.exception("[%s] collection crashed", target.name)
            return failed
        finally:
            if memo is not None and mac_recorder is not None:
                _record_mac_snapshot(mac_recorder, target, runner, memo, audit)
            runner.close()
    finally:
        sent = list(getattr(runner, "command_log", []) or [])
        ctx.commands[target.name] = [vars(r) for r in sent]
        progress.device_done(audit, len(sent))


def _record_mac_snapshot(recorder: mac_check.SnapshotRecorder, target: SwitchTarget,
                         runner: BaseRunner, memo: mac_check.AnswerMemo,
                         audit: SwitchAudit) -> None:
    """migration-check's commands on the session that is still open. Its failure
    is the MAC snapshot's problem, never the collection's."""
    try:
        recorder.record(target, runner, memo)
    except Exception as exc:  # broad on purpose: the audit is already complete
        log.exception("[%s] MAC snapshot failed", target.name)
        audit.warnings.append(f"MAC snapshot (migration-check) failed: "
                              f"{exc.__class__.__name__}: {exc}")


def run_collection(targets: list[SwitchTarget], creds: Credentials,
                   ctx: CollectContext) -> list[SwitchAudit]:
    """Every target in parallel, name-sorted."""
    audits: list[SwitchAudit] = []
    with cf.ThreadPoolExecutor(max_workers=ctx.cfg.ssh.workers) as pool:
        futures = [pool.submit(collect_one_switch, t, creds, ctx) for t in targets]
        for future in cf.as_completed(futures):
            audits.append(future.result())
    audits.sort(key=lambda a: a.name)
    return audits


def collect_fabric(dvrs: list[DvrTarget], creds: Credentials, cfg: Config,
                   opts: CollectOptions,
                   progress: NullProgress | None = None) -> FabricState:
    """The fabric as the DvR controllers see it, merged across all of them."""
    # fabric-wide listings (isis spbm i-sid) can be large - give the DvR
    # sessions extra read-timeout headroom
    dvr_cfg = replace(cfg, ssh=replace(cfg.ssh,
                                       read_timeout=max(cfg.ssh.read_timeout, 120)))
    tracker = progress or NullProgress()

    def collect_one(dvr: DvrTarget) -> FabricState:
        fragment = FabricState()
        try:
            runner = make_runner(dvr, Platform.VOSS, creds, dvr_cfg, opts)
        except Exception as exc:  # broad on purpose: keep reading the other DvRs
            fragment.dvr_errors.append(f"{dvr.name}: {exc}")
            log.error("%s: %s", dvr.name, exc)
            tracker.fabric_done(dvr.name, False)
            return fragment
        try:
            collect_dvr(dvr.name, runner, fragment)
        except Exception as exc:  # broad on purpose: keep reading the other DvRs
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


def collect(targets: list[SwitchTarget], creds: Credentials, dvr_creds: Credentials,
            ctx: CollectContext) -> Collected:
    """Switches and, with `opts.fabric`, the DvR controllers. The two reads are
    independent - only the comparison needs both - so they overlap and the
    fabric round-trip leaves the critical path. The MAC snapshot is finished
    here, before anyone looks at the fabric result: a baseline taken of the
    switches is worth keeping even when the fabric could not be read."""
    cfg = ctx.cfg
    fabric = FabricState()
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        future = (pool.submit(collect_fabric, cfg.dvr_controllers, dvr_creds, cfg,
                              ctx.opts, ctx.progress)
                  if ctx.opts.fabric and cfg.dvr_controllers else None)
        audits = run_collection(targets, creds, ctx)
        if future is not None:
            fabric = future.result()
    if ctx.mac_recorder is not None:
        ctx.mac_recorder.finish()
    return Collected(audits, fabric, ctx.commands)


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

def preview_commands(targets: list[SwitchTarget], cfg: Config, opts: CollectOptions,
                     dvrs: list[DvrTarget] | None = None,
                     mac_for: Collection[str] = ()) -> dict[str, list[str]]:
    """--dry-run: what each device WOULD be asked, without connecting.

    Produced by running the REAL collectors against a runner that connects to
    nothing and answers every command with an empty string. So the list cannot
    drift away from what the tool does, and because empty output sends the
    collectors down every fallback branch it is the full set of commands that
    could be sent - not just the subset one release accepts.

    `mac_for` names the switches a MAC snapshot would be recorded for; their
    list includes migration-check's commands, minus those already sent.
    """
    preview: dict[str, list[str]] = {}
    for target in targets:
        runner = DryRunRunner(target.name, target.platform,
                              command_overrides=cfg.command_overrides)
        memo = mac_check.AnswerMemo(runner) if target.name in mac_for else None
        try:
            # a preview drops nothing: what matters is the commands, not the text
            collect_switch(target, memo or runner, cfg, replace(opts.reads, keep_config=True))
            if memo is not None:
                mac_check.send_remaining(target.platform, runner, memo)
        except Exception:  # broad on purpose: a preview must never fail the run
            log.exception("[%s] dry-run preview incomplete", target.name)
        preview[target.name] = runner.commands
    for dvr in (dvrs or []):
        runner = DryRunRunner(dvr.name, Platform.VOSS,
                              command_overrides=cfg.command_overrides)
        try:
            collect_dvr(dvr.name, runner, FabricState())
        except Exception:  # broad on purpose: same
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
