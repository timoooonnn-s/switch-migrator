"""Live progress for the collection phase.

A run sends around sixteen commands to every VOSS box, and the per-port FDB
fallback can add one per up port on top of that - minutes of silence behind a
single spinner that only ever said "Auditing...". This shows which devices are
being read right now and which command each one is on, keeps a bar for the
overall count, and prints one line per finished device.

Nothing here is load-bearing: NullProgress has the same interface and does
nothing, which is what non-interactive runs and the tests use.
"""

from __future__ import annotations

import threading

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from switch_migrator.models import SwitchAudit


class NullProgress:
    """Same interface, no output."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fabric_done(self, name: str, ok: bool) -> None:
        pass

    def device_start(self, name: str) -> None:
        pass

    def device_command(self, name: str, command: str, index: int) -> None:
        pass

    def device_done(self, audit: SwitchAudit, commands: int) -> None:
        pass


class CollectionProgress(NullProgress):
    def __init__(self, console: Console, switches: int, dvrs: int = 0):
        self._console = console
        self._lock = threading.Lock()
        self._tasks: dict[str, int] = {}
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=24),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self._switches = switches
        self._dvrs = dvrs
        self._overall = None
        self._fabric = None

    def __enter__(self):
        self._progress.start()
        if self._dvrs:
            self._fabric = self._progress.add_task(
                "Fabric state from DvR controllers", total=self._dvrs)
        self._overall = self._progress.add_task(
            f"Auditing {self._switches} switch(es)", total=self._switches)
        return self

    def __exit__(self, *exc):
        self._progress.stop()
        return False

    def fabric_done(self, name: str, ok: bool) -> None:
        with self._lock:
            if self._fabric is not None:
                self._progress.advance(self._fabric)
        mark = "[green]ok[/green]" if ok else "[red]failed[/red]"
        self._console.print(f"  DvR {name}: {mark}")

    # --- per switch --------------------------------------------------------

    def device_start(self, name: str) -> None:
        with self._lock:
            self._tasks[name] = self._progress.add_task(
                f"  [cyan]{name}[/cyan] connecting", total=None)

    def device_command(self, name: str, command: str, index: int) -> None:
        with self._lock:
            task = self._tasks.get(name)
            if task is not None:
                self._progress.update(
                    task, description=f"  [cyan]{name}[/cyan] {command}",
                    completed=index)

    def device_done(self, audit: SwitchAudit, commands: int) -> None:
        with self._lock:
            task = self._tasks.pop(audit.name, None)
            if task is not None:
                self._progress.remove_task(task)
            if self._overall is not None:
                self._progress.advance(self._overall)
        if audit.reachable:
            self._console.print(
                f"  [green]done[/green] {audit.name}: {len(audit.ports)} port(s), "
                f"{len(audit.mlts)} MLT(s), {len(audit.vlans)} VLAN(s), "
                f"{commands} command(s)"
                + (f" [yellow]{len(audit.warnings)} warning(s)[/yellow]"
                   if audit.warnings else ""))


def make_progress(console: Console, switches: int, dvrs: int = 0,
                  enabled: bool = True) -> NullProgress:
    """A live progress display on a terminal, a silent one everywhere else."""
    if not enabled or not console.is_terminal:
        return NullProgress()
    return CollectionProgress(console, switches, dvrs)
