"""Named connection/scenario profiles.

A profile bundles the things one recurring scenario always needs - the
inventory, the output directory, the collection settings, the target switch -
so "scenario A" is one load away instead of six prompts. Loading a profile
only SETS the session/run values; everything stays changeable on the fly
afterwards, and nothing in a profile is ever a credential.

Stored as plain YAML next to the config, in the tool's home directory
(`paths.py`; default `profiles.yaml` there), so it can be reviewed, diffed and
handed around like the rest of the config. Every path in a profile is resolved
relative to the profiles file itself, so a profile means the same thing from
any working directory:

    profiles:
      site-a:
        inventory: inventories/site-a.yaml
        output_dir: output/site-a
        new_switch: leaf-a-01
        save_raw: true
        manifest: true
        auto_snapshot: true
        split_by_location: true
        location_groups:
          Frankfurt: [gx-11, gx-12]
        offline: null            # or a raw-capture dir to replay
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from switch_migrator import paths


def default_file() -> Path:
    """`profiles.yaml` in the tool's home directory."""
    return paths.profiles_file()

# the keys a profile may carry, with their coercions
_STR_KEYS = ("new_switch",)
_BOOL_KEYS = ("save_raw", "manifest", "auto_snapshot", "split_by_location")
_PATH_KEYS = ("inventory", "output_dir", "offline")


class ProfileError(Exception):
    pass


def load_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """All profiles in the file, name -> settings dict. Missing file = none."""
    path = default_file() if path is None else path
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from None
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise ProfileError(f"{path}: 'profiles' must be a mapping of "
                           f"name -> settings")
    out: dict[str, dict[str, Any]] = {}
    for name, raw in profiles.items():
        if not isinstance(raw, dict):
            raise ProfileError(f"{path}: profile '{name}' must be a mapping")
        out[str(name)] = _clean(raw, where=f"{path}: profile '{name}'",
                                beside=path.parent)
    return out


def _clean(raw: dict[str, Any], where: str, beside: Path) -> dict[str, Any]:
    """Keep only known keys, with the right types; unknown keys are reported
    once rather than silently dropped (a typo should not lose a setting)."""
    known = set(_STR_KEYS) | set(_BOOL_KEYS) | set(_PATH_KEYS) | {
        "location_groups"}
    unknown = [k for k in raw if k not in known]
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) {', '.join(sorted(unknown))} "
                           f"(known: {', '.join(sorted(known))})")
    out: dict[str, Any] = {}
    for key in _STR_KEYS:
        if raw.get(key) is not None:
            out[key] = str(raw[key])
    for key in _BOOL_KEYS:
        if raw.get(key) is not None:
            out[key] = bool(raw[key])
    for key in _PATH_KEYS:
        if raw.get(key):
            # relative to the profiles file, like the config's `inventory:` -
            # a profile is read from wherever the tool was started
            value = Path(str(raw[key])).expanduser()
            out[key] = value if value.is_absolute() else beside / value
    groups = raw.get("location_groups")
    if groups:
        if not isinstance(groups, dict):
            raise ProfileError(f"{where}: location_groups must be a mapping "
                               f"of name -> [locations]")
        out["location_groups"] = {
            str(g): [str(m) for m in (v if isinstance(v, list) else [v])]
            for g, v in groups.items()}
    return out


def save_profile(name: str, settings: dict[str, Any],
                 path: Path | None = None) -> Path:
    """Add or replace one profile, keeping every other profile in the file."""
    path = default_file() if path is None else path
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ProfileError(f"{path}: invalid YAML: {exc}") from None
        if not isinstance(data, dict):
            data = {}
    profiles = data.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ProfileError(f"{path}: 'profiles' must be a mapping")
    plain = {}
    for key, value in settings.items():
        if isinstance(value, Path):
            plain[key] = str(value)
        else:
            plain[key] = value
    profiles[name] = plain
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False,
                                   default_flow_style=False),
                    encoding="utf-8")
    return path
