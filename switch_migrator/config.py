"""Configuration & inventory loading with validation.

Credentials never live in config files. They come from environment variables
(SM_USERNAME / SM_PASSWORD, optionally SM_DVR_USERNAME / SM_DVR_PASSWORD) or
from an interactive prompt.
"""

from __future__ import annotations

import getpass
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from switch_migrator.models import Platform


class ConfigError(Exception):
    pass


@dataclass
class DvrTarget:
    name: str
    host: str


@dataclass
class SwitchTarget:
    name: str
    host: str
    platform: Platform


@dataclass
class SshSettings:
    conn_timeout: int = 20
    read_timeout: int = 60
    workers: int = 4
    retries: int = 1


@dataclass
class Credentials:
    username: str
    password: str


@dataclass
class Config:
    dvr_controllers: list[DvrTarget]
    core_switch_patterns: list[str]
    isid_offsets: list[int]
    isid_explicit: dict[int, int]
    excluded_vlans: set[int]
    ssh: SshSettings = field(default_factory=SshSettings)


def _require(data: dict, key: str, path: Path):
    if key not in data or data[key] in (None, [], {}):
        raise ConfigError(f"{path}: missing or empty required key '{key}'")
    return data[key]


def load_config(path: Path) -> Config:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    dvrs = []
    for i, entry in enumerate(_require(data, "dvr_controllers", path)):
        if not isinstance(entry, dict) or "host" not in entry:
            raise ConfigError(f"{path}: dvr_controllers[{i}] needs at least 'host'")
        dvrs.append(DvrTarget(name=str(entry.get("name", entry["host"])), host=str(entry["host"])))

    conventions = data.get("isid_conventions") or {}
    offsets = [int(o) for o in (conventions.get("offsets") or [])]
    explicit = {int(k): int(v) for k, v in (conventions.get("explicit") or {}).items()}
    if not offsets and not explicit:
        raise ConfigError(
            f"{path}: isid_conventions must define at least one offset or explicit mapping"
        )

    ssh_data = data.get("ssh") or {}
    ssh = SshSettings(
        conn_timeout=int(ssh_data.get("conn_timeout", 20)),
        read_timeout=int(ssh_data.get("read_timeout", 60)),
        workers=max(1, int(ssh_data.get("workers", 4))),
        retries=max(0, int(ssh_data.get("retries", 1))),
    )

    return Config(
        dvr_controllers=dvrs,
        core_switch_patterns=[str(p) for p in (data.get("core_switch_patterns") or [])],
        isid_offsets=offsets,
        isid_explicit=explicit,
        excluded_vlans={int(v) for v in (data.get("excluded_vlans") or [])},
        ssh=ssh,
    )


def load_inventory(path: Path) -> list[SwitchTarget]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"inventory file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None

    entries = data.get("switches") if isinstance(data, dict) else data
    if not entries:
        raise ConfigError(f"{path}: no switches defined")

    targets = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "name" not in entry or "platform" not in entry:
            raise ConfigError(f"{path}: switches[{i}] needs 'name' and 'platform'")
        targets.append(_make_target(str(entry["name"]), str(entry.get("host") or entry["name"]),
                                    str(entry["platform"]), where=f"{path}: switches[{i}]"))
    return targets


def parse_switch_arg(arg: str) -> SwitchTarget:
    """Parse a CLI -s argument: NAME:PLATFORM[:HOST]."""
    parts = arg.split(":")
    if len(parts) < 2:
        raise ConfigError(f"-s '{arg}': expected NAME:PLATFORM[:HOST] (e.g. old-sw-01:ers)")
    name, platform = parts[0], parts[1]
    host = parts[2] if len(parts) > 2 else name
    return _make_target(name, host, platform, where=f"-s '{arg}'")


def _make_target(name: str, host: str, platform: str, where: str) -> SwitchTarget:
    try:
        plat = Platform(platform.strip().lower())
    except ValueError:
        raise ConfigError(f"{where}: platform must be 'voss' or 'ers', got '{platform}'") from None
    return SwitchTarget(name=name.strip(), host=host.strip(), platform=plat)


def get_credentials(role: str, env_prefix: str, fallback: Credentials | None = None,
                    interactive: bool = True) -> Credentials:
    """Resolve credentials for `role` ('switches' or 'DvR controllers').

    Order: environment variables -> fallback (reuse switch creds for DvR controllers) ->
    interactive prompt.
    """
    user = os.environ.get(f"{env_prefix}_USERNAME")
    pw = os.environ.get(f"{env_prefix}_PASSWORD")
    if user and pw:
        return Credentials(user, pw)
    if fallback is not None:
        return fallback
    if not interactive or not sys.stdin.isatty():
        raise ConfigError(
            f"no credentials for {role}: set {env_prefix}_USERNAME and "
            f"{env_prefix}_PASSWORD (no TTY available for prompting)"
        )
    print(f"Credentials for {role}:", file=sys.stderr)
    user = input(f"  {role} username: ")
    pw = getpass.getpass(f"  {role} password: ")
    if not user or not pw:
        raise ConfigError(f"empty credentials for {role}")
    return Credentials(user, pw)
