"""The MAC-level before/after check, done by migration-check.

The verification (`verify.py`) checks every link a technician wrote on the sheet.
migration-check adds the view across all switches: is every end-device MAC the old
switches learned back *anywhere*, in the right VLAN, on the port the sheet says - and
if not, is it missing, in another VLAN, behind an uplink, or on another port (swapped
cables). This module is the only place switch-migrator touches migration-check:

* `SnapshotRecorder` writes a migration-check snapshot from the sessions this tool
  already has open - for the baseline (`--mac-snapshot`, old switches) and for the
  after side (`--verify-migration --mac-baseline`, new switches). The legacy SSH
  algorithms, the console server and `--offline` replay all apply, and nothing
  connects twice. A command the collection already sent is not sent again.
* `check()` compares the baseline(s) with the after snapshot, with the sheet's
  migrated rows as the port map, optionally re-reading the new switches until the
  result settles (`--mac-watch`), and writes the report as text and JSON.

Since D14 `migration_check` is a package of this repository rather than a separate
distribution, so there is no version gate and no optional-dependency dance. The
layering is unchanged and enforced by a test: switch_migrator -> migration_check
-> fabric_weft, never back (migration-check decision 0001). That is what keeps
`migration-check` usable on its own, from the same install.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import json
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migration_check.capture import looks_rejected
from migration_check.compare import Comparison, LoadedSnapshot, Verdict, compare, load
from migration_check.model import Platform as CheckPlatform
from migration_check.platforms import ADAPTERS
from migration_check.report import render_text, to_json, trend_line
from migration_check.snapshot import CommandStatus, SnapshotWriter
from migration_check.watch import round_folder
from migration_check.watch import watch as run_watch
from switch_migrator.cabling_sheet import Sheet
from switch_migrator.config import ConfigError, MacCheckSettings, SwitchTarget
from switch_migrator.connection import BaseRunner, CommandError, looks_like_error
from switch_migrator.models import Platform

log = logging.getLogger(__name__)

# raw files that only a switch-migrator VOSS capture holds, and the one
# migration-check needs instead - a baseline with the first and not the second
# has a MAC table migration-check cannot read
_VOSS_FDB_FILE = "show_interfaces_gigabitethernet_fdb_entry.txt"
_FE_MAC_FILE = "show_vlan_mac_address_entry.txt"


def unreadable_baselines(baselines: list[Path]) -> list[str]:
    """Device folders of a baseline whose MAC table migration-check cannot read.

    A `--save-raw` capture of an old VOSS switch holds
    `show interfaces gigabitEthernet fdb-entry`; migration-check reads
    `show vlan mac-address-entry`. Every MAC of such a device would silently drop
    out of the comparison, so it is named before anything runs.
    """
    out = []
    for base in baselines:
        for device in sorted(p for p in base.iterdir() if p.is_dir()):
            if (device / _VOSS_FDB_FILE).is_file() and not (device / _FE_MAC_FILE).is_file():
                out.append(f"{base}/{device.name}")
    return out


def commands_for(platform: Platform) -> tuple[str, ...]:
    """migration-check's command list for one platform. Read-only by its own
    contract; checked here as well, because this tool's read-only claim covers
    every command it sends, whoever chose it."""

    commands: tuple[str, ...] = ADAPTERS[CheckPlatform.from_name(platform.value)].commands
    rogue = [c for c in commands if not c.strip().lower().startswith("show ")]
    if rogue:
        raise ConfigError(f"migration-check asks for non-show command(s) {rogue} - refused")
    return commands


def port_map(sheet: Sheet | None) -> dict[tuple[str, str], tuple[str, str]]:
    """(old switch, old port) -> (new switch, new port) for every row whose cable
    has moved. The sheet reader has already normalised the ports.

    No sheet (an offline comparison of two folders) means no mapping: a MAC is then
    expected back anywhere in the right VLAN, and `OTHER_PORT` cannot be told from
    `ELSEWHERE`. Giving the sheet is what makes the verdicts port-exact.
    """
    if sheet is None:
        return {}
    return {(row.old_switch, row.old_port): (row.new_switch, row.new_port)
            for row in sheet.moved}


# --------------------------------------------------------------------------
# recording a snapshot on sessions that are already open
# --------------------------------------------------------------------------

class AnswerMemo:
    """Stands in for a runner during collection and remembers every answer.

    The collectors call `run` exactly as they would on the runner. Afterwards the
    recorder takes the commands migration-check also needs from here instead of
    sending them again: on ERS that is all five of them. A rejected command is
    remembered too (as the switch's error text); a command that got no answer is
    not, so the recorder tries it once more.
    """

    def __init__(self, runner: BaseRunner):
        self._runner = runner
        self.answers: dict[str, str] = {}

    @property
    def setup_warnings(self) -> list[str]:
        return getattr(self._runner, "setup_warnings", [])

    def run(self, command: str) -> str:
        try:
            output = self._runner.run(command)
        except CommandError as error:
            if looks_like_error(error.output or ""):
                self.answers[command] = error.output
            raise
        self.answers[command] = output
        return output


def _answer(runner: BaseRunner, memo: AnswerMemo | None, command: str) -> str:
    """The switch's text for `command`, an error answer included (migration-check
    records it as rejected). Raises only when no answer came back."""
    if memo is not None and command in memo.answers:
        return memo.answers[command]
    try:
        return runner.run(command)
    except CommandError as error:
        if looks_like_error(error.output or ""):
            return error.output
        raise


def send_remaining(platform: Platform, runner: BaseRunner, memo: AnswerMemo) -> None:
    """Send migration-check's commands the collection has not sent yet, discarding
    the answers (`--dry-run`: what the MAC snapshot would add to the list)."""
    for command in commands_for(platform):
        with contextlib.suppress(CommandError):
            _answer(runner, memo, command)


class SnapshotRecorder:
    """Writes one migration-check snapshot from switch-migrator's sessions.

    Thread-safe: the collection records every device from its own worker thread.
    `only` limits it to some switches - a verification run may also have read the
    old switches, which are the baseline, not the result.
    """

    def __init__(self, path: Path, label: str,
                 only: Collection[str] | None = None):

        self.path = path
        self.only = set(only) if only is not None else None
        self._writer = SnapshotWriter(path, label)

    def wants(self, name: str) -> bool:
        return self.only is None or name in self.only

    def record(self, target: SwitchTarget, runner: BaseRunner,
               memo: AnswerMemo | None = None) -> None:

        if not self.wants(target.name):
            return
        self._writer.add_device(target.name, CheckPlatform.from_name(target.platform.value),
                                target.host)
        for command in commands_for(target.platform):
            try:
                text = _answer(runner, memo, command)
            except CommandError as error:
                self._writer.save(target.name, command, None, CommandStatus.FAILED,
                                  (error.output or str(error))[:200])
                continue
            rejection = looks_rejected(text) or (
                text.strip().splitlines()[0] if looks_like_error(text) else None)
            self._writer.save(target.name, command, text,
                              CommandStatus.REJECTED if rejection else CommandStatus.OK,
                              rejection)

    def unreachable(self, target: SwitchTarget, reason: str) -> None:
        """A switch the collection could not reach is in the snapshot too, with every
        command failed - so the report says so instead of the switch just missing."""

        if not self.wants(target.name):
            return
        self._writer.add_device(target.name, CheckPlatform.from_name(target.platform.value),
                                target.host)
        for command in commands_for(target.platform):
            self._writer.save(target.name, command, None, CommandStatus.FAILED,
                              f"connect: {reason}"[:200])

    def finish(self) -> Path:
        self._writer.finish()
        return self.path


def capture(targets: list[SwitchTarget], folder: Path,
            open_runner: Callable[[SwitchTarget], BaseRunner],
            label: str, workers: int = 4) -> Path:
    """A snapshot of `targets` on fresh sessions (the later rounds of --mac-watch)."""
    recorder = SnapshotRecorder(folder, label)

    def one(target: SwitchTarget) -> None:
        try:
            runner = open_runner(target)
        except Exception as exc:  # broad on purpose: one dead switch must not cost the round
            recorder.unreachable(target, f"{exc.__class__.__name__}: {exc}")
            return
        try:
            recorder.record(target, runner)
        finally:
            runner.close()

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, targets))
    return recorder.finish()


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------

def after_snapshot_path(output_dir: Path, stamp: str, watching: bool) -> Path:
    """Where the after snapshot of a verification run goes. With --mac-watch it is
    the first of several rounds, in migration-check's round layout."""
    after = output_dir / f"mac-check-{stamp}" / "after"
    if watching:
        first: Path = round_folder(after, 1)
        return first
    return after


@dataclass
class MacCheck:
    comparison: Comparison
    snapshot: Path          # the last after snapshot taken
    text: Path
    json: Path
    trend: list[str] = field(default_factory=list)   # one line per watch round
    stopped: str = ""                                # why the watch stopped

    @property
    def settled(self) -> bool:
        return not (self.comparison.not_found or self.comparison.vlans_gone)

    @property
    def summary(self) -> str:

        worst_first = (Verdict.MISSING, Verdict.WRONG_VLAN, Verdict.OTHER_PORT,
                       Verdict.ELSEWHERE)
        counts = ", ".join(f"{len(self.comparison.with_verdict(v))} {v.value}"
                           for v in worst_first)
        not_found = len(self.comparison.not_found)
        total = len(self.comparison.results)
        head = (f"{not_found} of {total} edge MACs not back" if not_found
                else f"all {total} edge MACs back")
        return f"{head} ({counts})"


def load_baselines(baselines: list[Path]) -> list[LoadedSnapshot]:

    return [load(path) for path in baselines]


def _ignore_line(line: str) -> None:
    """The default round callback: say nothing."""


@dataclass
class MacCheckJob:
    """What one MAC check compares, and where its report goes."""
    baselines: list[Path]
    after: Path                  # the new switches' snapshot, taken during verification
    sheet: Sheet | None          # the port map; None = compare two folders as they are
    output_dir: Path
    stamp: str


@dataclass
class Watching:
    """How --mac-watch re-reads the new switches between rounds."""
    settings: MacCheckSettings
    targets: list[SwitchTarget]
    open_runner: Callable[[SwitchTarget], BaseRunner]
    workers: int = 4
    on_round: Callable[[str], None] = _ignore_line


@dataclass
class _Outcome:
    comparison: Comparison
    last: Path                   # the after snapshot the result comes from
    trend: list[str] = field(default_factory=list)
    stopped: str = ""


def check(job: MacCheckJob, watch: Watching | None = None) -> MacCheck:
    """Compare the baseline(s) with the after snapshot that was recorded during the
    verification and write the report.

    With `watch`, `job.after` is round 1 and the new switches are read again on
    fresh sessions every interval until everything is back, the number not back
    stops falling, or the round limit is reached (migration-check decision 0011).
    """
    before = load_baselines(job.baselines)
    mapping = port_map(job.sheet)
    if watch is None:
        outcome = _Outcome(compare(before, load(job.after), port_map=mapping), job.after)
    else:
        outcome = _watch(job, watch, before, mapping)
    return _write_report(job, outcome)


def _watch(job: MacCheckJob, watch: Watching, before: list[LoadedSnapshot],
           mapping: dict[tuple[str, str], tuple[str, str]]) -> _Outcome:
    trend: list[str] = []
    done: list[Any] = []          # migration-check's watch rounds

    def take(folder: Path) -> None:
        if folder != job.after:      # round 1 was recorded during the verification
            capture(watch.targets, folder, watch.open_runner, label="after",
                    workers=watch.workers)

    def on(rounds: tuple[Any, ...]) -> None:
        done[:] = rounds
        line = trend_line(rounds)
        trend.append(line)
        watch.on_round(line)

    settings = watch.settings
    try:
        result = run_watch(
            take_snapshot=take,
            compare_round=lambda folder: compare(before, load(folder), port_map=mapping),
            out=job.after.parent, interval_seconds=settings.interval_seconds,
            max_rounds=settings.max_rounds, stable_rounds=settings.stable_rounds,
            on_round=on)
    except KeyboardInterrupt:
        # Ctrl-C ends the watch, not the run: the last finished round is the
        # result, and the verification's other reports still get written
        if done:
            return _Outcome(done[-1].comparison, done[-1].path, trend,
                            f"stopped after round {done[-1].number}: interrupted")
        return _Outcome(compare(before, load(job.after), port_map=mapping), job.after,
                        trend, "stopped in round 1: interrupted")
    return _Outcome(result.last.comparison, result.last.path, trend,
                    f"stopped after round {result.last.number}: {result.reason.value}")


def _write_report(job: MacCheckJob, outcome: _Outcome) -> MacCheck:
    text = job.output_dir / f"mac-check-{job.stamp}.txt"
    head = "".join(f"{line}\n" for line in outcome.trend)
    if outcome.stopped:
        head += f"{outcome.stopped}\n\n"
    text.write_text(head + render_text(outcome.comparison), encoding="utf-8")
    json_path = job.output_dir / f"mac-check-{job.stamp}.json"
    payload = to_json(outcome.comparison)
    if outcome.trend:
        payload["watch"] = {"rounds": outcome.trend, "stopped": outcome.stopped}
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return MacCheck(outcome.comparison, outcome.last, text, json_path, outcome.trend,
                    outcome.stopped)
