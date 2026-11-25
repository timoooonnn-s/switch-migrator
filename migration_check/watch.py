"""Re-take the after snapshot until the result settles (decision 0011).

The first after snapshot always over-reports: quiet devices have not sent a frame yet.
`watch` takes a new snapshot every interval and compares it with the same baseline. It
stops when everything is found, when the number not found has not fallen for
`stable_rounds` rounds, or after `max_rounds`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from migration_check.compare import Comparison


class StopReason(StrEnum):
    ALL_FOUND = "everything found"
    STABLE = "the number not found stopped falling"
    ROUND_LIMIT = "round limit reached"


@dataclass(frozen=True, slots=True)
class Round:
    number: int
    path: Path
    comparison: Comparison


@dataclass(frozen=True, slots=True)
class WatchResult:
    rounds: tuple[Round, ...]
    reason: StopReason

    @property
    def last(self) -> Round:
        return self.rounds[-1]


def round_folder(out: Path, number: int) -> Path:
    return out / f"round-{number:02d}"


def watch(
    *,
    take_snapshot: Callable[[Path], object],
    compare_round: Callable[[Path], Comparison],
    out: Path,
    interval_seconds: float,
    max_rounds: int,
    stable_rounds: int,
    on_round: Callable[[tuple[Round, ...]], None] = lambda rounds: None,
    sleep: Callable[[float], None] = time.sleep,
) -> WatchResult:
    """`take_snapshot(folder)` writes one after snapshot; `compare_round(folder)`
    compares it with the baseline. Both are passed in, so switch-migrator can use its
    own sessions and the tests no switch at all."""
    if max_rounds < 1 or stable_rounds < 1:
        raise ValueError("rounds and stable rounds must be at least 1")
    rounds: list[Round] = []
    fewest_not_found: int | None = None
    rounds_without_progress = 0
    for number in range(1, max_rounds + 1):
        folder = round_folder(out, number)
        take_snapshot(folder)
        comparison = compare_round(folder)
        rounds.append(Round(number, folder, comparison))
        on_round(tuple(rounds))

        not_found = len(comparison.not_found)
        if not_found == 0 and not comparison.vlans_gone:
            return WatchResult(tuple(rounds), StopReason.ALL_FOUND)
        if fewest_not_found is None or not_found < fewest_not_found:
            fewest_not_found, rounds_without_progress = not_found, 0
        else:
            rounds_without_progress += 1
        if rounds_without_progress >= stable_rounds:
            return WatchResult(tuple(rounds), StopReason.STABLE)
        if number < max_rounds:
            sleep(interval_seconds)
    return WatchResult(tuple(rounds), StopReason.ROUND_LIMIT)
