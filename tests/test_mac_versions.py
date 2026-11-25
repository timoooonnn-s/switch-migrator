"""One repository, one version (D14): switch_migrator, migration_check and
pyproject.toml must agree, and `tools/make-bundle.py` must not go looking for
migration-check in a sibling checkout any more."""

from __future__ import annotations

import re
from pathlib import Path

import migration_check
import switch_migrator

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\[project\][^\[]*?^version = "([^"]+)"', text, re.MULTILINE | re.DOTALL)
    assert match, "pyproject.toml has no [project] version"
    return match.group(1)


def test_the_three_versions_agree() -> None:
    assert migration_check.__version__ == switch_migrator.__version__ == _pyproject_version()


def test_the_bundle_does_not_vendor_migration_check() -> None:
    text = (ROOT / "tools" / "make-bundle.py").read_text(encoding="utf-8")
    assert "pre-migration-checker" not in text
    assert "vendor/migration-check" not in text


def test_migration_check_imports_nothing_from_switch_migrator() -> None:
    """Decision 0001, still true after the merge: the package can be lifted out."""
    importing = re.compile(r"^\s*(?:from|import)\s+switch_migrator\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "migration_check").rglob("*.py"))
        if importing.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
