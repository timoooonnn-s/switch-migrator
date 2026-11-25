"""migration-check snap | show | diff | watch.

Exit codes: 0 clean, 1 something to look at (a MAC not found, a VLAN gone, a problem
reading a snapshot), 2 the command could not run (bad option, unreadable snapshot).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from migration_check import __version__
from migration_check.capture import (
    Connect,
    Target,
    capture,
    netmiko_sessions,
    parse_target,
    read_inventory,
)
from migration_check.compare import Comparison, LoadedSnapshot, PortMap, compare, load
from migration_check.model import Platform, PortKey
from migration_check.portmap import read_port_map
from migration_check.report import render_snapshot, render_text, to_json, trend_line
from migration_check.snapshot import SnapshotError
from migration_check.watch import watch

EXIT_CLEAN, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.run(args))
    except (SnapshotError, ValueError, OSError) as error:
        print(f"migration-check: {error}", file=sys.stderr)
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migration-check",
        description="Snapshot switches before and after a change, then compare by MAC.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    snap = commands.add_parser("snap", help="save the switches' show output into a new folder")
    snap.add_argument("out", type=Path, help="new, empty folder for the snapshot")
    snap.add_argument("--label", help="e.g. pre, post, pre-day-before")
    _add_target_options(snap)
    snap.set_defaults(run=_run_snap)

    show = commands.add_parser("show", help="what a snapshot holds, per device")
    show.add_argument("snapshot", type=Path)
    _add_reading_options(show)
    show.set_defaults(run=_run_show)

    diff = commands.add_parser("diff", help="compare snapshots: before (one or more) vs after")
    diff.add_argument("--after", type=Path, required=True)
    _add_compare_options(diff)
    diff.set_defaults(run=_run_diff)

    watching = commands.add_parser(
        "watch", help="re-snap the after side every interval until the result settles"
    )
    watching.add_argument("out", type=Path, help="folder for the rounds (round-01, ...)")
    watching.add_argument("--interval", type=float, default=120, help="seconds between rounds")
    watching.add_argument("--rounds", type=int, default=15, help="at most this many rounds")
    watching.add_argument(
        "--stable", type=int, default=3, help="stop after this many rounds without progress"
    )
    _add_target_options(watching)
    _add_compare_options(watching)
    watching.set_defaults(run=_run_watch)
    return parser


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--inventory", type=Path, help="switch-migrator switches.yaml, or CSV")
    parser.add_argument(
        "--device", action="append", default=[], metavar="HOST[=NAME]", help="repeatable"
    )
    parser.add_argument("--platform", type=Platform.from_name, help="for --device: boss | fe")
    parser.add_argument("--username", default=os.environ.get("MIGRATION_CHECK_USERNAME"))
    parser.add_argument("--workers", type=int, default=4, help="switches read in parallel")


def _add_reading_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--assume-platform",
        type=Platform.from_name,
        help="for snapshot folders whose files do not tell the platform",
    )
    parser.add_argument(
        "--uplink", action="append", default=[], type=_port_key, metavar="DEVICE:PORT",
        help="treat this port as an uplink (its MACs are not compared)",
    )  # fmt: skip
    parser.add_argument(
        "--edge", action="append", default=[], type=_port_key, metavar="DEVICE:PORT",
        help="treat this port as an access port, whatever LLDP says",
    )  # fmt: skip


def _add_compare_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--before", type=Path, action="append", required=True, help="repeatable: merged baseline"
    )
    parser.add_argument(
        "--vlan-map", action="append", default=[], metavar="OLD=NEW", help="renumbered VLANs"
    )
    parser.add_argument(
        "--port-map", type=Path, help="CSV: Old switch, Old port, New switch, New port"
    )
    parser.add_argument("--json", type=Path, help="also write the full result as JSON here")
    _add_reading_options(parser)


def _port_key(text: str) -> PortKey:
    device, colon, port = text.rpartition(":")
    if not colon or not device or not port:
        raise argparse.ArgumentTypeError(f"{text!r}: expected DEVICE:PORT")
    return device, port


def _vlan_map(pairs: Sequence[str]) -> dict[int, int]:
    mapping = {}
    for pair in pairs:
        old, equals, new = pair.partition("=")
        if not equals or not old.strip().isdigit() or not new.strip().isdigit():
            raise ValueError(f"--vlan-map {pair!r}: expected OLD=NEW, e.g. 735=1735")
        mapping[int(old)] = int(new)
    return mapping


def _port_map(path: Path | None) -> PortMap:
    if path is None:
        return {}
    port_map = read_port_map(path)
    for problem in port_map.problems:
        _log(f"port map: {problem}")
    return port_map.mapping


def _targets(args: argparse.Namespace) -> list[Target]:
    targets = read_inventory(args.inventory) if args.inventory else []
    if args.device:
        if args.platform is None:
            raise ValueError("--device needs --platform")
        targets += [parse_target(spec, args.platform) for spec in args.device]
    if not targets:
        raise ValueError("nothing to capture: give --inventory or --device")
    return targets


def _sessions(args: argparse.Namespace) -> Connect:
    username = args.username or getpass.getuser()
    password = os.environ.get("MIGRATION_CHECK_PASSWORD") or getpass.getpass(
        f"password for {username}: "
    )
    return netmiko_sessions(username, password, log=_log)


def _log(line: str) -> None:
    print(line, file=sys.stderr)


def _load(path: Path, args: argparse.Namespace) -> LoadedSnapshot:
    return load(path, platform=args.assume_platform, uplinks=args.uplink, edges=args.edge)


def _finish(comparison: Comparison, args: argparse.Namespace) -> int:
    print(render_text(comparison), end="")
    if args.json:
        args.json.write_text(json.dumps(to_json(comparison), indent=2) + "\n", encoding="utf-8")
    return EXIT_CLEAN if comparison.clean else EXIT_FINDINGS


def _run_snap(args: argparse.Namespace) -> int:
    targets = _targets(args)
    capture(targets, args.out, _sessions(args), label=args.label, workers=args.workers, log=_log)
    loaded = load(args.out)
    print(render_snapshot(loaded), end="")
    _log(f"saved: {args.out}")
    return EXIT_FINDINGS if loaded.problems else EXIT_CLEAN


def _run_show(args: argparse.Namespace) -> int:
    loaded = _load(args.snapshot, args)
    print(render_snapshot(loaded), end="")
    return EXIT_FINDINGS if loaded.problems else EXIT_CLEAN


def _run_diff(args: argparse.Namespace) -> int:
    before = [_load(path, args) for path in args.before]
    comparison = compare(
        before, _load(args.after, args), _vlan_map(args.vlan_map), _port_map(args.port_map)
    )
    return _finish(comparison, args)


def _run_watch(args: argparse.Namespace) -> int:
    # everything that can be wrong is checked before the first switch is touched
    targets = _targets(args)
    before = [_load(path, args) for path in args.before]
    vlan_map, port_map = _vlan_map(args.vlan_map), _port_map(args.port_map)
    connect = _sessions(args)
    result = watch(
        take_snapshot=lambda folder: capture(
            targets, folder, connect, label="after", workers=args.workers, log=_log
        ),
        compare_round=lambda folder: compare(before, _load(folder, args), vlan_map, port_map),
        out=args.out,
        interval_seconds=args.interval,
        max_rounds=args.rounds,
        stable_rounds=args.stable,
        on_round=lambda rounds: _log(trend_line(rounds)),
    )
    _log(f"stopped after round {result.last.number}: {result.reason.value}\n")
    return _finish(result.last.comparison, args)


if __name__ == "__main__":
    sys.exit(main())
