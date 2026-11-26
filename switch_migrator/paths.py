"""Where the tool's own files live.

switch-migrator is installed once and run from wherever the engineer happens to
stand - a change window is no time to remember which directory the config was
in. So nothing of the tool's own is looked up relative to the current working
directory. The config, the default inventory and the profiles come from one
**home directory**, found in a fixed order:

    1. $SM_HOME                            when set, that directory and nothing else
    2. $XDG_CONFIG_HOME/switch-migrator    (default ~/.config/switch-migrator)
    3. /etc/switch-migrator                a server-wide deployment
    4. the installation directory          the checkout or unpacked bundle this
                                           package runs from, when it carries a
                                           config.yaml of its own

The first of those that holds a `config.yaml` is the home; a per-user config
therefore wins over the server's, and both win over the install folder. With
none of them holding one, the home is the install folder when the tool runs
from a checkout or an unpacked bundle (which is where `cp config.example.yaml
config.yaml` puts it), and the user's config directory otherwise - so the
"config file not found" error names the place the file should be created, and
`looked_in()` names every place that was tried.

Reports go to `<home>/output`, or to `$SM_OUTPUT_DIR`; a home that cannot be
written to (`/etc/switch-migrator` for a non-root user) sends them to
`~/switch-migrator/output` instead, because a run must not fail on the
directory it was going to write its log into.

A path the user types stays the user's: it is relative to their shell's working
directory, as every other command's arguments are. `input_path()` only adds a
second place to look - the home directory - for a relative path that is not
there, so `switch-migrator -i switches.yaml` finds the inventory next to the
config from anywhere on the server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

HOME_ENV = "SM_HOME"
OUTPUT_ENV = "SM_OUTPUT_DIR"
XDG_ENV = "XDG_CONFIG_HOME"

CONFIG_NAME = "config.yaml"
INVENTORY_NAME = "switches.yaml"
PROFILES_NAME = "profiles.yaml"
OUTPUT_NAME = "output"

SYSTEM_HOME = Path("/etc/switch-migrator")
# where the reports go when the home directory is read-only (a packaged
# /etc deployment run by a normal user)
USER_FALLBACK = Path("switch-migrator")

# os.environ, or a mapping standing in for it in a test
Env = Mapping[str, str]


def install_dir() -> Path:
    """The directory this package runs from: a checkout, an unpacked bundle,
    or (for a non-editable install) site-packages."""
    return Path(__file__).resolve().parents[1]


def _installed_in_place() -> bool:
    """True when the installation directory is a checkout or an unpacked
    bundle - somewhere a config.yaml belongs - rather than site-packages."""
    return (install_dir() / "pyproject.toml").is_file()


def looked_in(env: Env | None = None) -> list[Path]:
    """Every directory the home may be, in order. With $SM_HOME set, just it:
    an explicit home is never second-guessed, so a typo in it is an error the
    user can see rather than a silent fallback to someone else's config."""
    env = os.environ if env is None else env
    explicit = (env.get(HOME_ENV) or "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    xdg = (env.get(XDG_ENV) or "").strip()
    user = (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "switch-migrator"
    return [user, SYSTEM_HOME, install_dir()]


def home(env: Env | None = None) -> Path:
    """The tool's home directory (see the module docstring)."""
    places = looked_in(env)
    for place in places:
        if (place / CONFIG_NAME).is_file():
            return place
    if len(places) > 1 and _installed_in_place():
        return places[-1]
    return places[0]


def config_file(env: Env | None = None) -> Path:
    return home(env) / CONFIG_NAME


def profiles_file(env: Env | None = None) -> Path:
    return home(env) / PROFILES_NAME


def inventory_file(env: Env | None = None) -> Path:
    return home(env) / INVENTORY_NAME


def output_root(env: Env | None = None) -> Path:
    """Where reports, logs and snapshots go when -o says nothing."""
    env = os.environ if env is None else env
    explicit = (env.get(OUTPUT_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    place = home(env)
    if os.access(place, os.W_OK):
        return place / OUTPUT_NAME
    return Path.home() / USER_FALLBACK / OUTPUT_NAME


def input_path(path: Path, env: Env | None = None) -> Path:
    """A file the user named, found. An absolute path, or a relative one that
    is there, is taken as given; a relative one that is not is tried in the
    home directory, so the files that live next to the config can be named by
    their bare name from any working directory. A path that is nowhere comes
    back as it was typed, so the error names what the user asked for."""
    if path.is_absolute() or path.exists():
        return path
    candidate = home(env) / path
    return candidate if candidate.exists() else path


def missing_config(path: Path, env: Env | None = None) -> str:
    """The message for a config that is not there: what is missing, where the
    tool looked, and how to say it outright."""
    places = looked_in(env)
    tried = (f"looked in: {', '.join(str(p) for p in places)}"
             if path == config_file(env)
             else f"the home directory is {home(env)}")
    return (f"config file not found: {path}\n"
            f"  {tried}\n"
            f"  create it ('cp config.example.yaml {CONFIG_NAME}'), point "
            f"{HOME_ENV} at the directory that holds it, or name it with -c")
