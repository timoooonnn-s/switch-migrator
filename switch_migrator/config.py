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

from switch_migrator.location import LocationRules
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
    # console-server line/port id. When set (and console_server is configured)
    # the tool reaches this switch through the terminal server instead of a
    # direct SSH to `host` - for boxes with no management IP yet.
    console: str = ""


@dataclass
class ConsoleServerSettings:
    """How to reach a switch's serial console through a terminal server.

    Only the mechanics live here; WHICH line a switch hangs off is the
    per-switch `console:` field in the inventory. Two common flavors, both
    template-driven so the exact scheme stays in the config:

    * username-embedded (Avocent-style `ssh user:7003@tsserver`):
        username_template: "{username}:70{port}"
    * TCP-port-per-line (OpenGear-style `ssh -p 3003 tsserver`):
        tcp_port_template: "30{port}"

    `{port}` is the switch's `console:` value; `{username}` the login name.
    """
    host: str = ""
    username_template: str = ""       # e.g. "{username}:70{port}"
    tcp_port_template: str = ""       # e.g. "70{port}" -> SSH TCP port
    device_type: str = "generic_termserver"   # netmiko terminal-server driver

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def resolve(self, username: str, console: str) -> tuple[str, int]:
        """(login username, TCP port) for one console line."""
        user = username
        if self.username_template:
            user = self.username_template.format(username=username,
                                                 port=console)
        port = 22
        if self.tcp_port_template:
            port = int(self.tcp_port_template.format(port=console))
        return user, port


@dataclass
class SshSettings:
    conn_timeout: int = 20
    read_timeout: int = 60
    workers: int = 4
    retries: int = 1                # reconnect attempts after a connect failure
    # re-sends of a single command that died on a transport error (a stuck
    # pager, one dropped read). A command the DEVICE rejected is never retried
    # - that answer will not change.
    command_retries: int = 1
    # consecutive transport failures after which the session is abandoned
    max_transport_failures: int = 2
    legacy_algorithms: bool = True  # old ERS/BOSS kex/ciphers/host keys
    global_delay_factor: float = 1.0  # slow gear: raise to give reads more time
    default_enter: str | None = None  # e.g. "\r\n" for ERS/BOSS that ignore "\n"
    # advanced: paramiko disabled_algorithms, e.g. {"kex": ["curve25519-..."]}
    # to pin OUT a modern algorithm a device negotiates but implements badly
    disabled_algorithms: dict | None = None


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
    excluded_vlan_names: list[str] = field(default_factory=list)
    # a port down longer than this counts as unused (see usage.py)
    unused_after_days: int = 30
    # learned MACs kept per port for the migration sheets. Raise it on
    # server-heavy access ports, lower it on busy uplinks - the full count is
    # always reported as '(+N more)' regardless.
    mac_cap: int = 10
    # how switch names map to sites, and which sites share a cabling worksheet
    locations: LocationRules = field(default_factory=LocationRules)
    ssh: SshSettings = field(default_factory=SshSettings)
    # default inventory file, so a plain `switch-migrator` needs no -i;
    # resolved relative to the config file's own directory
    inventory: Path | None = None
    # per-command spelling overrides ({'show mlt': 'show mlt all', ...}),
    # applied at the runner - replacements are restricted to read-only
    # spellings so the tool's licence to run in a change window survives
    command_overrides: dict[str, str] = field(default_factory=dict)
    # optional terminal server for switches with a `console:` line
    console_server: ConsoleServerSettings = field(
        default_factory=ConsoleServerSettings)


def _require(data: dict, key: str, path: Path):
    if key not in data or data[key] in (None, [], {}):
        raise ConfigError(f"{path}: missing or empty required key '{key}'")
    return data[key]


def load_config(path: Path, require_fabric: bool = True) -> Config:
    """Load the config. With require_fabric=False (the --no-fabric report mode)
    the DvR controllers and I-SID conventions are optional: the tool only reads
    and reports per-switch state and never compares against a fabric.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    dvr_entries = (_require(data, "dvr_controllers", path) if require_fabric
                   else (data.get("dvr_controllers") or []))
    dvrs = []
    for i, entry in enumerate(dvr_entries):
        if not isinstance(entry, dict) or "host" not in entry:
            raise ConfigError(f"{path}: dvr_controllers[{i}] needs at least 'host'")
        dvrs.append(DvrTarget(name=str(entry.get("name", entry["host"])), host=str(entry["host"])))

    conventions = data.get("isid_conventions") or {}
    offsets = [int(o) for o in (conventions.get("offsets") or [])]
    explicit = {int(k): int(v) for k, v in (conventions.get("explicit") or {}).items()}
    if require_fabric and not offsets and not explicit:
        raise ConfigError(
            f"{path}: isid_conventions must define at least one offset or explicit mapping"
        )

    ssh_data = data.get("ssh") or {}
    disabled_algorithms = ssh_data.get("disabled_algorithms")
    if disabled_algorithms is not None and not isinstance(disabled_algorithms, dict):
        raise ConfigError(
            f"{path}: ssh.disabled_algorithms must be a mapping like "
            f"{{kex: [...], ciphers: [...], keys: [...]}}")
    default_enter = ssh_data.get("default_enter")
    ssh = SshSettings(
        conn_timeout=int(ssh_data.get("conn_timeout", 20)),
        read_timeout=int(ssh_data.get("read_timeout", 60)),
        workers=max(1, int(ssh_data.get("workers", 4))),
        retries=max(0, int(ssh_data.get("retries", 1))),
        command_retries=max(0, int(ssh_data.get("command_retries", 1))),
        max_transport_failures=max(1, int(ssh_data.get("max_transport_failures", 2))),
        legacy_algorithms=bool(ssh_data.get("legacy_algorithms", True)),
        global_delay_factor=float(ssh_data.get("global_delay_factor", 1.0)),
        default_enter=str(default_enter) if default_enter else None,
        disabled_algorithms=disabled_algorithms,
    )

    excluded_vlans: set[int] = set()
    for v in data.get("excluded_vlans") or []:
        try:
            excluded_vlans.add(int(v))
        except (TypeError, ValueError):
            raise ConfigError(
                f"{path}: excluded_vlans entry '{v}' is not a VLAN id - to "
                f"exclude VLANs by NAME (e.g. 'quarantaine') use the "
                f"excluded_vlan_names list instead") from None

    loc = data.get("locations") or {}
    if not isinstance(loc, dict):
        raise ConfigError(f"{path}: 'locations' must be a mapping with "
                          f"'patterns', 'fallback_segments' and/or 'groups'")
    patterns = loc.get("patterns") or {}
    groups_raw = loc.get("groups") or {}
    if not isinstance(patterns, dict) or not isinstance(groups_raw, dict):
        raise ConfigError(f"{path}: locations.patterns and locations.groups "
                          f"must both be mappings")
    groups: dict[str, list[str]] = {}
    for name, members in groups_raw.items():
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list) or not members:
            raise ConfigError(
                f"{path}: locations.groups['{name}'] must be a non-empty list "
                f"of location names, e.g. ['Frankfurt DC1', 'Frankfurt DC2']")
        groups[str(name)] = [str(m) for m in members]
    locations = LocationRules(
        patterns={str(k): str(v) for k, v in patterns.items()},
        fallback_segments=max(1, int(loc.get("fallback_segments", 2))),
        groups=groups,
    )

    inventory = data.get("inventory")
    if inventory:
        inventory = Path(str(inventory))
        if not inventory.is_absolute():
            # relative to the config file, so `-c deploy/config.yaml` works
            # from any working directory
            inventory = path.parent / inventory

    overrides_raw = data.get("commands") or {}
    if not isinstance(overrides_raw, dict):
        raise ConfigError(f"{path}: 'commands' must be a mapping of "
                          f"original -> replacement command")
    command_overrides: dict[str, str] = {}
    for orig, repl in overrides_raw.items():
        repl = str(repl).strip()
        if not repl.startswith(("show", "terminal", "term ", "enable")):
            raise ConfigError(
                f"{path}: commands['{orig}'] = '{repl}' is not a read-only "
                f"command - overrides must start with 'show', 'terminal'/"
                f"'term' or 'enable', because the tool's read-only claim "
                f"covers everything it sends")
        command_overrides[str(orig).strip()] = repl

    cs_raw = data.get("console_server") or {}
    if not isinstance(cs_raw, dict):
        raise ConfigError(f"{path}: 'console_server' must be a mapping with "
                          f"'host' and a username_template/tcp_port_template")
    console_server = ConsoleServerSettings(
        host=str(cs_raw.get("host") or ""),
        username_template=str(cs_raw.get("username_template") or ""),
        tcp_port_template=str(cs_raw.get("tcp_port_template") or ""),
        device_type=str(cs_raw.get("device_type") or "generic_termserver"),
    )

    return Config(
        dvr_controllers=dvrs,
        core_switch_patterns=[str(p) for p in (data.get("core_switch_patterns") or [])],
        isid_offsets=offsets,
        isid_explicit=explicit,
        excluded_vlans=excluded_vlans,
        excluded_vlan_names=[str(p) for p in (data.get("excluded_vlan_names") or [])],
        unused_after_days=int(data.get("unused_after_days", 30)),
        mac_cap=max(1, int(data.get("mac_cap", 10))),
        locations=locations,
        ssh=ssh,
        inventory=inventory,
        command_overrides=command_overrides,
        console_server=console_server,
    )


def load_inventory(path: Path) -> list[SwitchTarget]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        target = _make_target(str(entry["name"]), str(entry.get("host") or entry["name"]),
                              str(entry["platform"]), where=f"{path}: switches[{i}]")
        if entry.get("console") is not None:
            # console-server line for boxes with no management IP (yet);
            # needs the config's console_server section to take effect
            target.console = str(entry["console"]).strip()
        targets.append(target)
    return targets


def default_inventory(cfg: "Config") -> Path | None:
    """The inventory to use when none is given: the config's `inventory:` key,
    else a `switches.yaml` sitting in the working directory."""
    if cfg.inventory is not None:
        return cfg.inventory
    fallback = Path("switches.yaml")
    return fallback if fallback.is_file() else None


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
