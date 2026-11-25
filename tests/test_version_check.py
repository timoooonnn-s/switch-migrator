"""Is this checkout behind its upstream? (version_check.py)"""

import shutil
import subprocess
from pathlib import Path

import pytest

from switch_migrator import version_check

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


NEW_ON_ORIGIN = 2         # commits 'two' and 'three'


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-qm", name)


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "one")
    _git(tmp_path, "clone", "-q", str(origin), "work")
    return origin, tmp_path / "work"


def test_up_to_date_says_nothing(clone):
    _, work = clone
    _git(work, "fetch", "-q")
    state = version_check.checkout_state(work)
    assert state is not None
    assert state.behind == 0
    assert state.warning() == ""


def test_behind_after_a_fetch_says_how_far(clone):
    origin, work = clone
    _commit(origin, "two")
    _commit(origin, "three")
    _git(work, "fetch", "-q")          # the tool itself never fetches
    state = version_check.checkout_state(work)
    assert state is not None
    assert state.behind == NEW_ON_ORIGIN
    assert "2 commit(s) behind origin/main" in state.warning()


def test_not_a_checkout_is_silence(tmp_path):
    assert version_check.checkout_state(tmp_path) is None
