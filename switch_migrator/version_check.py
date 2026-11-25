"""Is this the version the plan was made with? (council review, Field Engineer 6)

A migration once ran from a checkout eleven commits behind its own repository,
and nothing said so. When the tool runs from a git checkout, this compares it
with its upstream branch - as of the LAST `git fetch`: nothing is fetched, no
network is touched, nothing is written. Anything it cannot tell (not a checkout,
no git, no upstream) is silence, never an error.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 2.0
# a fetch older than this cannot vouch for "up to date"
STALE_FETCH_DAYS = 14
_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class CheckoutState:
    behind: int                      # commits HEAD lacks from its upstream
    upstream: str                    # e.g. 'origin/main'
    fetched_days_ago: int | None     # age of the last fetch, None if never

    def warning(self) -> str:
        """One line for the console, or '' when there is nothing to say."""
        when = ("" if self.fetched_days_ago is None
                else f", as of the last git fetch {self.fetched_days_ago} day(s) ago")
        if self.behind:
            return (f"This checkout is {self.behind} commit(s) behind {self.upstream}"
                    f"{when} - run 'git pull' before a migration.")
        if self.fetched_days_ago is None or self.fetched_days_ago > STALE_FETCH_DAYS:
            return (f"Up to date with {self.upstream} only as far as the last git "
                    f"fetch knows{when or ' (never fetched)'} - 'git fetch' to check.")
        return ""


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def checkout_state(root: Path | None = None) -> CheckoutState | None:
    """The state of the checkout this package runs from, or None."""
    root = root or Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        return None                  # installed from a wheel, or a copied folder
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    behind = _git(root, "rev-list", "--count", "HEAD..@{u}")
    if not upstream or behind is None or not behind.isdigit():
        return None
    fetch_head = root / ".git" / "FETCH_HEAD"
    days = (int((time.time() - fetch_head.stat().st_mtime) // _SECONDS_PER_DAY)
            if fetch_head.exists() else None)
    return CheckoutState(int(behind), upstream, days)
