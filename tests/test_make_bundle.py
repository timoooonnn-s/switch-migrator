"""tools/make-bundle.py: the bundle unpacks to exactly the shipped files, updates a copy
in place without touching the site's own files, and refuses a damaged copy."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "make-bundle.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_bundle", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module       # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[ModuleType, Path]:
    mb = _load()
    missing = [str(p.root) for p in mb.PARTS if not (p.root / ".git").exists()]
    if missing:
        pytest.skip(f"bundles are built from git checkouts: {', '.join(missing)}")
    out: Path = mb.build(tmp_path_factory.mktemp("dist"))
    return mb, out


def _run(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd, input="",
                          capture_output=True, text=True, check=False)


def _manifest(mb: ModuleType) -> str:
    return f".{mb.FOLDER}-files"


def test_it_unpacks_exactly_the_shipped_files(bundle: tuple[ModuleType, Path],
                                              tmp_path: Path) -> None:
    mb, script = bundle
    done = _run(script, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    target = tmp_path / mb.FOLDER
    expected = {p.prefix + n: p.root / n for p in mb.PARTS for n in mb.part_files(p)}
    found = {f.relative_to(target).as_posix() for f in target.rglob("*") if f.is_file()}
    assert found - {_manifest(mb)} == set(expected)
    for name, source in expected.items():
        assert (target / name).read_bytes() == source.read_bytes(), name


def test_an_update_keeps_the_sites_files_and_drops_what_is_gone(
        bundle: tuple[ModuleType, Path], tmp_path: Path) -> None:
    mb, script = bundle
    _run(script, cwd=tmp_path)
    target = tmp_path / mb.FOLDER
    own = target / ".venv" / "pyvenv.cfg"               # the site's: never touched
    own.parent.mkdir()
    own.write_text("mine")
    gone = target / "dropped-in-this-version.txt"       # an earlier bundle's file
    gone.write_text("old")
    with (target / _manifest(mb)).open("a") as manifest:
        manifest.write("dropped-in-this-version.txt\n.venv/pyvenv.cfg\n")

    done = _run(script, cwd=target)                      # inside a copy: update in place
    assert done.returncode == 0, done.stderr
    assert own.read_text() == "mine"
    assert not gone.exists()
    assert "removed dropped-in-this-version.txt" in done.stdout
    assert "file(s) unchanged" in done.stdout


def test_a_damaged_bundle_writes_nothing(bundle: tuple[ModuleType, Path],
                                         tmp_path: Path) -> None:
    mb, script = bundle
    damaged = tmp_path / "damaged.py"
    damaged.write_text(script.read_text().replace('SHA256 = "', 'SHA256 = "0', 1))
    done = _run(damaged, cwd=tmp_path)
    assert done.returncode != 0
    assert "damaged" in done.stderr
    assert not (tmp_path / mb.FOLDER).exists()


def test_the_same_contents_give_the_same_bundle(bundle: tuple[ModuleType, Path],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    mb, script = bundle
    assert mb.build(script.parent) == script
    assert "already up to date" in capsys.readouterr().out
