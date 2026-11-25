#!/usr/bin/env python3
"""Pack switch-migrator into ONE self-extracting text file, e.g. to send it by mail.

    python3 tools/make-bundle.py            ->  dist/switch-migrator-bundle-<version>.py
    python3 tools/make-bundle.py --force    ->  replace a bundle of the same name
    python3 tools/make-bundle.py --out DIR  ->  write it to DIR instead of dist/

The file name carries the version (__version__ in switch_migrator/__init__.py, which must
match pyproject.toml), so bump it before shipping changed contents: two different bundles
must never share a name, or someone installs the wrong one.

On the target machine:

    python3 switch-migrator-bundle-<version>.py            # inside an existing copy (or its
                                                            # git checkout): update in place;
                                                            # elsewhere: a new ./switch-migrator
    python3 switch-migrator-bundle-<version>.py /srv/sm    # unpack / update exactly there
    python3 switch-migrator-bundle-<version>.py --list     # only show what is inside

The bundle is plain ASCII (a base64-encoded tar.gz) with a SHA-256 checksum, so a damaged
attachment is detected before anything is written. It never contains, overwrites or removes
the site's own files (config.yaml, switches.yaml, inventories, profiles.yaml, output/,
snapshots, .venv), so it is also the way to update. Only files under git are packed - as
they are on disk, uncommitted edits included (they are listed) - and council/ stays home.

Building needs git. Unpacking needs Python 3.8+ and nothing else; the Python packages the
tool depends on come from pip afterwards, and the bundle prints the command. The same
contents always give the same checksum, so rebuilding an unchanged tree is a no-op.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Part:
    """One git checkout that goes into the bundle."""

    label: str           # its name, as the bundle's title gives it
    root: Path
    prefix: str          # where its files land inside the bundle; "" = the top level
    ship: str            # regex: which of its tracked files are shipped
    version_file: str    # holds its __version__


# --- what this bundle is (the only part that differs between the projects) ----------------
FOLDER = "switch-migrator"              # the folder a fresh unpack creates
PARTS = (
    Part("switch-migrator", ROOT, "", r"^(?!council/)", "switch_migrator/__init__.py"),
)
# the site's own files: never packed, never overwritten, never removed by an update
SITE_FILES = (
    r"(^|/)(config\.yaml|switches\.yaml|profiles\.yaml|output|snapshots?|raw|dist|\.git"
    r"|\.venv|venv|\.claude|__pycache__|\.DS_Store)(/|$)"
    r"|(^|/)inventory(?![^/]*\.example\.)[^/]*\.ya?ml$"
    r"|(^|/)(snapshot|manifest|mac-check)[^/]*\.json$|\.(pyc|log|xlsx)$"
)
REQUIRES = (3, 10)                      # the tool's own Python floor (pyproject)
SETUP = "-e ."                          # pip install arguments, run inside the target
EXAMPLES = (("config.yaml", "config.example.yaml"),
            ("switches.yaml", "switches.example.yaml"))
CHECK = ".venv/bin/switch-migrator --version"
NOTES = (
    "MAC check (optional, Python 3.11+): unpack the pre-migration-checker bundle too, then",
    "  .venv/bin/pip install -e <its folder>/vendor/fabric-weft -e <its folder>",
)
# ------------------------------------------------------------------------------------------

_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)
_PYPROJECT_VERSION_RE = re.compile(r'^\[project\][^\[]*?^version = "([^"]+)"',
                                   re.MULTILINE | re.DOTALL)
_PAYLOAD_LINE = 76


class BundleError(Exception):
    """The bundle cannot be built as asked."""


def _git(root: Path, *args: str) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), *args, "-z"], capture_output=True,
                          check=False)
    if done.returncode:
        raise BundleError(f"{root}: git {' '.join(args)} failed - bundles are built from "
                          f"a git checkout ({done.stderr.decode(errors='replace').strip()})")
    return [n for n in done.stdout.decode("utf-8").split("\0") if n]


def part_files(part: Part) -> list[str]:
    """The part's shipped files, relative to its root. What is left out and why is said."""
    site = re.compile(SITE_FILES)
    ship = re.compile(part.ship)
    for name in _git(part.root, "ls-files", "--others", "--exclude-standard"):
        print(f"  not included ({part.label}, not in git - commit it to ship it): {name}")
    for name in _git(part.root, "ls-files", "--modified"):
        print(f"  packed with UNCOMMITTED changes ({part.label}): {name}")
    names = []
    for name in _git(part.root, "ls-files", "--cached"):
        if not ship.search(name) or not (part.root / name).is_file():
            continue
        if site.search(name):
            print(f"  not included ({part.label}, a site file even though tracked): {name}")
            continue
        names.append(name)
    return sorted(names)


def part_version(part: Part) -> str:
    """__version__ of the part, which must agree with its pyproject.toml."""
    text = (part.root / part.version_file).read_text(encoding="utf-8")
    found = _VERSION_RE.search(text)
    if found is None:
        raise BundleError(f"no __version__ in {part.root / part.version_file}")
    pyproject = (part.root / "pyproject.toml").read_text(encoding="utf-8")
    declared = _PYPROJECT_VERSION_RE.search(pyproject)
    if declared is not None and declared.group(1) != found.group(1):
        raise BundleError(f"{part.label}: {part.version_file} says {found.group(1)}, "
                          f"pyproject.toml says {declared.group(1)} - make them agree")
    return found.group(1)


def pack(files: list[tuple[Path, str]]) -> bytes:
    """A tar.gz of (path on disk, name in the bundle). Deterministic: no dates, no owners,
    so the same contents always give the same bytes and the same checksum."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, name in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), compresslevel=9, mtime=0)


def render(name: str, version: str, title: str, raw: bytes) -> str:
    """The extractor script with everything filled in."""
    payload = base64.b64encode(raw).decode("ascii")
    values = {
        "TITLE": title,
        "NAME": name,
        "FOLDER": FOLDER,
        "VERSION": version,
        "VERSION_FILE": PARTS[0].version_file,
        "MANIFEST": f".{FOLDER}-files",
        "CREATED": time.strftime("%Y-%m-%d %H:%M"),
        "SHA256": hashlib.sha256(raw).hexdigest(),
        "SITE_FILES": repr(SITE_FILES),
        "REQUIRES": repr(REQUIRES),
        "SETUP": SETUP,
        "EXAMPLES": repr(EXAMPLES),
        "CHECK": CHECK,
        "NOTES": repr(NOTES),
    }
    text = EXTRACTOR
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    lines = (payload[i:i + _PAYLOAD_LINE] for i in range(0, len(payload), _PAYLOAD_LINE))
    return text.replace("@@PAYLOAD@@", "\n".join(lines))


def existing_sha(path: Path) -> str | None:
    """The checksum recorded in a bundle that is already there, or None."""
    try:
        head = path.read_text(encoding="ascii", errors="replace")[:8192]
    except OSError:
        return None
    found = re.search(r'^SHA256 = "([0-9a-f]+)"', head, re.MULTILINE)
    return found.group(1) if found else None


def build(out_dir: Path, force: bool = False) -> Path:
    """Write the bundle; returns its path."""
    files: list[tuple[Path, str]] = []
    versions = []
    for part in PARTS:
        versions.append((part.label, part_version(part)))
        files += [(part.root / n, part.prefix + n) for n in part_files(part)]
    version = versions[0][1]
    name = "-".join([f"{FOLDER}-bundle-{version}",
                     *(f"{label}-{ver}" for label, ver in versions[1:])]) + ".py"
    title = " + ".join(f"{label} {ver}" for label, ver in versions)
    raw = pack(files)
    digest = hashlib.sha256(raw).hexdigest()
    out = out_dir / name
    old = existing_sha(out)
    if old == digest:
        print(f"{out} is already up to date (same contents).")
        return out
    if old and not force:
        raise BundleError(
            f"{out} exists already with DIFFERENT contents. Two bundles must never share "
            f"a name - someone would install the wrong one. Bump __version__ in "
            f"{PARTS[0].version_file} and pyproject.toml (now {version}), then build "
            f"again. --force overrides.")
    out_dir.mkdir(parents=True, exist_ok=True)
    text = render(name, version, title, raw)
    out.write_text(text, encoding="ascii", newline="\n")
    print(f"{title}: {len(files)} files -> {out} ({(len(text) + 1023) // 1024} KB)")
    print(f"SHA-256 of the contents: {digest}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="replace a bundle of the same name with different contents")
    parser.add_argument("--out", type=Path, default=ROOT / "dist",
                        help="where to write the bundle (default: dist/)")
    args = parser.parse_args(argv)
    try:
        build(args.out, force=args.force)
    except BundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


# The self-extracting script. Kept to Python 3.8 and the standard library: it is the
# one thing that has to run on a machine where nothing is installed yet.
EXTRACTOR = r'''#!/usr/bin/env python3
"""@@TITLE@@ - self-extracting bundle, created @@CREATED@@.

    python3 @@NAME@@
        inside a folder that already holds @@FOLDER@@ (e.g. its git checkout): update
        it in place; anywhere else: unpack into a new folder ./@@FOLDER@@
    python3 @@NAME@@ TARGET_DIR
        unpack / update exactly there
    python3 @@NAME@@ --list
        show the contents only

Files a newer version no longer has are removed - but only files a bundle installed
(listed in @@MANIFEST@@). .git, .venv, the site's own configuration and output, and any
other file of yours are never touched. Python 3.8+, standard library only.
"""
import base64
import hashlib
import io
import os
import re
import sys
import tarfile

FOLDER = "@@FOLDER@@"
VERSION = "@@VERSION@@"
SHA256 = "@@SHA256@@"
MANIFEST = "@@MANIFEST@@"
VERSION_FILE = "@@VERSION_FILE@@"
SITE_FILES = re.compile(@@SITE_FILES@@)
REQUIRES = @@REQUIRES@@
SETUP = "@@SETUP@@"
EXAMPLES = @@EXAMPLES@@
CHECK = "@@CHECK@@"
NOTES = @@NOTES@@
PAYLOAD = """
@@PAYLOAD@@
"""


def safe(name):
    """A plain relative name inside the target that is not a site file."""
    parts = name.split("/")
    return (bool(name) and not name.startswith("/") and "\\" not in name
            and ".." not in parts and name != MANIFEST and not SITE_FILES.search(name))


def installed_version(folder):
    """The version already in that folder, or None when there is none."""
    try:
        with open(os.path.join(folder, *VERSION_FILE.split("/")), encoding="utf-8") as f:
            found = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return found.group(1) if found else "?"


def as_numbers(version):
    return [int(p) if p.isdigit() else 0 for p in re.split(r"[._-]", version or "")]


def read_payload():
    raw = base64.b64decode("".join(PAYLOAD.split()))
    if hashlib.sha256(raw).hexdigest() != SHA256:
        sys.exit("ERROR: the bundle is damaged (checksum mismatch) - nothing was written. "
                 "Please send it again.")
    tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    members = tar.getmembers()
    for m in members:                       # only plain files with safe, relative names
        if not m.isfile() or not safe(m.name):
            sys.exit("ERROR: unexpected entry %r in the bundle - nothing was written." % m.name)
    return tar, members


def choose_target(argv):
    if len(argv) > 1:
        return os.path.abspath(argv[1])
    if installed_version(os.getcwd()):
        return os.getcwd()                  # run inside an existing copy: update it in place
    return os.path.abspath(FOLDER)


def confirm_downgrade(here):
    print("  WARNING: %s is already installed there - this bundle is OLDER (%s)." % (here, VERSION))
    try:
        answer = input("  Install the older version anyway? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


def write_files(tar, members, target):
    """Write what differs; returns the names that were created or updated."""
    changed = []
    for m in members:
        path = os.path.join(target, *m.name.split("/"))
        data = tar.extractfile(m).read()
        try:
            with open(path, "rb") as f:
                same = f.read() == data
        except OSError:
            same = None                     # a new file
        if not same:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as out:
                out.write(data)
            print("  %s %s" % ("updated" if same is False else "created", m.name))
            changed.append(m.name)
        os.chmod(path, 0o755 if m.mode & 0o111 else 0o644)
    return changed


def remove_dropped(target, names):
    """Remove what an earlier bundle installed and this one no longer has."""
    try:
        with open(os.path.join(target, MANIFEST), encoding="utf-8") as f:
            previous = set(line.strip() for line in f if safe(line.strip()))
    except OSError:
        previous = set()
    for name in sorted(previous - names):
        path = os.path.join(target, *name.split("/"))
        if os.path.isfile(path):
            os.remove(path)
            print("  removed %s" % name)
            folder = os.path.dirname(path)
            while folder != target and not os.listdir(folder):
                os.rmdir(folder)
                folder = os.path.dirname(folder)
    with open(os.path.join(target, MANIFEST), "w", encoding="utf-8") as f:
        f.write("".join(n + "\n" for n in sorted(names)))


def next_steps(target, changed):
    python = "python%d.%d" % REQUIRES
    if sys.version_info[:2] < REQUIRES:
        print("Python: this python3 is %d.%d; the tool needs %d.%d or newer - create the venv"
              " with that one (e.g. %s)." % (sys.version_info[0], sys.version_info[1],
                                               REQUIRES[0], REQUIRES[1], python))
    else:
        python = "python3"
    if not os.path.isdir(os.path.join(target, ".venv")):
        print("Install: cd %s && %s -m venv .venv && .venv/bin/pip install %s"
              % (target, python, SETUP))
    elif any(n.endswith("pyproject.toml") for n in changed):
        print("Update:  the dependencies changed - cd %s && .venv/bin/pip install %s"
              % (target, SETUP))
    else:
        print("Update:  .venv is there and the dependencies did not change - nothing to install.")
    for site, example in EXAMPLES:
        if not os.path.exists(os.path.join(target, site)) \
                and os.path.exists(os.path.join(target, example)):
            print("Config:  cd %s && cp %s %s   (then fill it in)" % (target, example, site))
    if os.path.isdir(os.path.join(target, ".git")):
        print("Git:     cd %s && git status   (then commit + push as usual)" % target)
    for line in NOTES:
        print(line)
    print("Check:   cd %s && %s" % (target, CHECK))


def main(argv):
    tar, members = read_payload()
    with tar:
        if argv[1:2] == ["--list"]:
            for m in members:
                print("%8d  %s" % (m.size, m.name))
            return
        target = choose_target(argv)
        here = installed_version(target)
        print("@@TITLE@@ -> %s%s\n" % (target, "   (replacing %s)" % here if here else ""))
        if here and as_numbers(here) > as_numbers(VERSION) and not confirm_downgrade(here):
            print("  Nothing changed.")
            return
        changed = write_files(tar, members, target)
    names = set(m.name for m in members)
    remove_dropped(target, names)
    unchanged = len(members) - len(changed)
    if unchanged:
        print("  %d file(s) unchanged" % unchanged)
    print("\n@@TITLE@@ is in %s\n" % target)
    next_steps(target, changed)


if __name__ == "__main__":
    main(sys.argv)
'''


if __name__ == "__main__":
    sys.exit(main())
