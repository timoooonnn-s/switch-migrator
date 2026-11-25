"""The MAC check (migration-check) as pipeline steps: which switches to record,
and the comparison after a verification, with its console output."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.console import Console

from switch_migrator import mac_check
from switch_migrator.cabling_sheet import Sheet
from switch_migrator.config import SwitchTarget


def mac_check_targets(targets: list[SwitchTarget], sheet: Sheet) -> list[SwitchTarget]:
    """Only the switches links moved onto: an inventory may also name the old
    switches, which are the baseline, not the result."""
    new_switches = set(sheet.by_new_switch())
    return [t for t in targets if t.name in new_switches] or targets


def after_recorder(output_dir: Path, stamp: str, watching: bool,
                   targets: list[SwitchTarget], sheet: Sheet
                   ) -> mac_check.SnapshotRecorder:
    """The new switches' snapshot, recorded during the verification's own
    collection (round 1 of a watch)."""
    return mac_check.SnapshotRecorder(
        mac_check.after_snapshot_path(output_dir, stamp, watching), label="after",
        only={t.name for t in mac_check_targets(targets, sheet)})


def run_mac_check(job: mac_check.MacCheckJob, watch: mac_check.Watching | None,
                  console: Console) -> mac_check.MacCheck:
    """The MAC comparison after a verification, with its console output. Only the
    switches links moved onto are re-read by a watch."""
    if watch is not None:
        settings = watch.settings
        console.print(f"MAC check: watching the new switches every "
                      f"{settings.interval_seconds:g} s, at most {settings.max_rounds} "
                      f"round(s) [dim](Ctrl-C stops)[/dim]")
        watch = replace(watch, targets=mac_check_targets(watch.targets, job.sheet),
                        on_round=lambda line: console.print(f"  {line.strip()}"))
    check = mac_check.check(job, watch)
    if check.stopped:
        console.print(f"  [dim]{check.stopped}[/dim]")
    color = "bold green" if check.settled else "bold yellow"
    console.print(f"[{color}]MAC check:[/{color}] {check.summary}")
    for _, device, problem in check.comparison.problems:
        console.print(f"  [yellow]{device}: {problem}[/yellow]")
    console.print(f"  [dim]details: {check.text}[/dim]")
    return check
