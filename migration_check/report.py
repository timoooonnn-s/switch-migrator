"""The comparison as plain text for a terminal on the jump server, and as JSON."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from typing import Any

from migration_check.compare import Comparison, LoadedSnapshot, MacResult, PortCheck, Verdict
from migration_check.model import DeviceState, MacSighting, Neighbor, PortKey, Role
from migration_check.watch import Round

NEW_MACS_SHOWN = 20

_VERDICT_LINES = (
    (Verdict.MISSING, "missing", "seen nowhere"),
    (Verdict.WRONG_VLAN, "wrong VLAN", "seen, but only in another VLAN"),
    (Verdict.OTHER_PORT, "other port", "on an access port, but not the one the port map says"),
    (Verdict.ELSEWHERE, "elsewhere", "same VLAN, but only over an uplink or through the fabric"),
    (Verdict.FOUND, "found", "on an access port, same VLAN"),
)


def render_text(comparison: Comparison) -> str:
    lines: list[str] = []
    before = ", ".join(loaded.snapshot.title for loaded in comparison.before)
    lines += [f"before: {before}", f"after:  {comparison.after.snapshot.title}", ""]

    total = len(comparison.results)
    not_found = len(comparison.not_found)
    lines.append(
        f"{not_found} of {total} edge MACs not seen after the change"
        if not_found
        else f"all {total} edge MACs seen after the change"
    )
    for verdict, label, meaning in _VERDICT_LINES:
        if verdict is Verdict.OTHER_PORT and not comparison.port_checks:
            continue  # without a port map there is no "other" port
        lines.append(f"  {label:<11}{len(comparison.with_verdict(verdict)):>6}  {meaning}")

    if comparison.problems:
        lines += _section("PROBLEMS: read these first, the numbers above may be incomplete")
        lines += [
            f"  {title} / {device}: {problem}" for title, device, problem in comparison.problems
        ]

    if comparison.vlans_gone:
        lines += _section("VLANS GONE: edge MACs before, the VLAN exists on no switch after")
        lines += [
            f"  VLAN {gone.vlan_id}{_paren(gone.name)}: {gone.edge_macs} edge MAC(s)"
            for gone in comparison.vlans_gone
        ]

    missing = comparison.with_verdict(Verdict.MISSING)
    if missing:
        lines += _section(f"MISSING ({len(missing)}), by old port")
        lines += _by_old_port(missing)

    wrong_vlan = comparison.with_verdict(Verdict.WRONG_VLAN)
    if wrong_vlan:
        lines += _section(f"WRONG VLAN ({len(wrong_vlan)})")
        lines += [_moved_line(result, show_vlan_after=True) for result in wrong_vlan]

    other_port = comparison.with_verdict(Verdict.OTHER_PORT)
    if other_port:
        lines += _section(f"OTHER PORT ({len(other_port)}): swapped cables, or the sheet is wrong")
        lines += [_other_port_line(result) for result in other_port]

    lines += _port_map_section(comparison.port_checks)

    elsewhere = comparison.with_verdict(Verdict.ELSEWHERE)
    if elsewhere:
        lines += _section(f"ELSEWHERE ({len(elsewhere)}): plugged in somewhere else?")
        lines += [_moved_line(result, show_vlan_after=False) for result in elsewhere]

    lines += _neighbor_section(comparison.lost_neighbors, comparison.new_neighbors)

    if comparison.new_macs:
        lines += _section(f"NEW EDGE MACS ({len(comparison.new_macs)}): not edge MACs before")
        lines += [
            f"  {s.mac}  VLAN {s.vlan_id:<5} {s.location}"
            for s in comparison.new_macs[:NEW_MACS_SHOWN]
        ]
        if len(comparison.new_macs) > NEW_MACS_SHOWN:
            lines.append(f"  ... {len(comparison.new_macs) - NEW_MACS_SHOWN} more in --json")

    lines += _section("DEVICES")
    lines += _device_table(comparison.before, comparison.after)

    lines += _section("WHAT THIS CHECK CANNOT SEE")
    lines += [f"  - {note}" for note in comparison.notes]
    return "\n".join(lines) + "\n"


def render_snapshot(loaded: LoadedSnapshot) -> str:
    """What one snapshot holds: enough to trust a baseline before the window opens."""
    lines = [loaded.snapshot.title, ""]
    lines += _device_table([], loaded, label_before="", label_after="")
    if loaded.problems:
        lines += _section("PROBLEMS")
        lines += [f"  {device}: {problem}" for device, problem in loaded.problems]
    for note in loaded.snapshot.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"


def _section(title: str) -> list[str]:
    return ["", title]


def _paren(text: str | None) -> str:
    return f" ({text})" if text else ""


def _by_old_port(results: Sequence[MacResult]) -> list[str]:
    groups: dict[str, list[MacResult]] = defaultdict(list)
    for result in results:
        groups[result.before.location].append(result)
    lines = []
    for location, members in groups.items():
        neighbor = next((r.neighbor for r in members if r.neighbor), None)
        lines.append(
            f"  {location}: {len(members)} MAC(s){_paren(neighbor and 'LLDP ' + neighbor)}"
        )
        lines += [
            f"      {r.before.mac}  VLAN {r.before.vlan_id:<5} {r.ip or ''}".rstrip()
            for r in members
        ]
    return lines


def _moved_line(result: MacResult, *, show_vlan_after: bool) -> str:
    after = "; ".join(
        f"{s.location}" + (f" VLAN {s.vlan_id}" if show_vlan_after else "") for s in result.after
    )
    return (
        f"  {result.before.mac}  VLAN {result.before.vlan_id:<5} "
        f"before {result.before.location}  ->  after {after}{_paren(result.ip)}"
    )


def _port(key: PortKey) -> str:
    return f"{key[0]} port {key[1]}"


def _other_port_line(result: MacResult) -> str:
    expected = " or ".join(_port(key) for key in result.expected_ports)
    found = "; ".join(s.location for s in result.after)
    return (
        f"  {result.before.mac}  VLAN {result.before.vlan_id:<5} {result.before.location}"
        f"  ->  sheet: {expected}, found: {found}"
    )


def _port_map_section(checks: Sequence[PortCheck]) -> list[str]:
    """Only the links with a problem: the per-link MAC check is switch-migrator's
    verification; this tool adds the scope-wide view (decision 0010)."""
    if not checks:
        return []
    with_problem = [check for check in checks if check.problem]
    up = sum(1 for check in checks if check.new_up)
    lines = _section(
        f"PORT MAP: {len(checks)} link(s), {up} new port(s) up, {len(with_problem)} with a problem"
    )
    lines += [
        f"  {_port(c.old)}  ->  {_port(c.new)}: {c.problem}"
        f" ({c.macs_before} edge MAC(s) before, {c.macs_back} back on the new port)"
        for c in with_problem
    ]
    return lines


def trend_line(rounds: Sequence[Round]) -> str:
    """One line per watch round: how many are still not found, and how many came back."""
    current = rounds[-1].comparison
    taken = current.after.snapshot.taken_at
    counts = ", ".join(
        f"{label} {len(current.with_verdict(verdict))}"
        for verdict, label, _ in _VERDICT_LINES
        if verdict is not Verdict.FOUND
        and (current.port_checks or verdict is not Verdict.OTHER_PORT)
    )
    line = (
        f"round {rounds[-1].number:>2}  {taken.strftime('%H:%M:%S') if taken else '--:--:--'}"
        f"  not found {len(current.not_found):>4} of {len(current.results)}  ({counts})"
    )
    if len(rounds) > 1:
        before = {(r.before.mac, r.before.vlan_id) for r in rounds[-2].comparison.not_found}
        now = {(r.before.mac, r.before.vlan_id) for r in current.not_found}
        line += f"  back since last round {len(before - now)}"
    if current.problems:
        line += f"  PROBLEMS {len(current.problems)}"
    return line


def _neighbor_section(lost: Sequence[Neighbor], new: Sequence[Neighbor]) -> list[str]:
    if not lost and not new:
        return []
    lines = _section("LLDP NEIGHBOURS")
    for label, neighbors in (("lost", lost), ("new ", new)):
        endpoints = [n for n in neighbors if not n.is_network_device]
        lines += [
            f"  {label}  {n.label}{_paren(n.capabilities)}  {n.device} port {n.local_port}"
            for n in endpoints
        ]
    network_lost = sorted({n.label for n in lost if n.is_network_device})
    network_new = sorted({n.label for n in new if n.is_network_device})
    if network_lost or network_new:
        lines.append(
            f"  switches/routers: lost {', '.join(network_lost) or '-'}; "
            f"new {', '.join(network_new) or '-'} (expected when switches are replaced)"
        )
    return lines


# MAC counts per role (decision 0004): a strange number here is how a wrong
# classification shows up.
_ROLE_COLUMNS = {
    Role.EDGE: "edge",
    Role.ACCESS_POINT: "wifi",
    Role.UPLINK: "uplink",
    Role.FABRIC: "fabric",
    Role.UNKNOWN: "unknown",
}


def _device_table(
    before: Iterable[LoadedSnapshot],
    after: LoadedSnapshot,
    label_before: str = "before",
    label_after: str = "after",
) -> list[str]:
    header = f"  {'':<7}{'device':<24}{'platform':<15}{'ports up':>9}" + "".join(
        f"{title:>8}" for title in _ROLE_COLUMNS.values()
    )
    lines = [header]
    for label, devices in (
        *((label_before, loaded.devices) for loaded in before),
        (label_after, after.devices),
    ):
        lines += [_device_row(label, device) for device in devices]
    return lines


def _device_row(label: str, device: DeviceState) -> str:
    roles = Counter(sighting.role for sighting in device.macs)
    return (
        f"  {label:<7}{device.name:<24}{device.platform.value:<15}{device.ports_up:>9}"
        + "".join(f"{roles.get(role, 0):>8}" for role in _ROLE_COLUMNS)
    )


def to_json(comparison: Comparison) -> dict[str, Any]:
    def sighting(s: MacSighting) -> dict[str, Any]:
        return asdict(s) | {"role": s.role.value, "location": s.location}

    def snapshot(loaded: LoadedSnapshot) -> dict[str, Any]:
        taken = loaded.snapshot.taken_at
        return {
            "path": str(loaded.snapshot.path),
            "label": loaded.snapshot.label,
            "taken_at": taken.isoformat() if taken else None,
            "devices": [
                {
                    "name": d.name,
                    "platform": d.platform.value,
                    "ports_up": d.ports_up,
                    "roles": dict(Counter(s.role.value for s in d.macs)),
                    "problems": list(d.problems),
                }
                for d in loaded.devices
            ],
        }

    return {
        "summary": {
            "edge_macs_before": len(comparison.results),
            **{v.value: len(comparison.with_verdict(v)) for v in Verdict},
            "new_edge_macs": len(comparison.new_macs),
            "clean": comparison.clean,
        },
        "before": [snapshot(loaded) for loaded in comparison.before],
        "after": snapshot(comparison.after),
        "results": [
            {
                "mac": r.before.mac,
                "verdict": r.verdict.value,
                "vlan_before": r.before.vlan_id,
                "vlan_expected": r.expected_vlan,
                "ip": r.ip,
                "neighbor": r.neighbor,
                "expected_ports": [list(key) for key in r.expected_ports],
                "before": sighting(r.before),
                "after": [sighting(s) for s in r.after],
            }
            for r in comparison.results
        ],
        "new_edge_macs": [sighting(s) for s in comparison.new_macs],
        "vlans_gone": [asdict(g) for g in comparison.vlans_gone],
        "lost_neighbors": [asdict(n) for n in comparison.lost_neighbors],
        "new_neighbors": [asdict(n) for n in comparison.new_neighbors],
        "vlan_map": {str(old): new for old, new in comparison.vlan_map.items()},
        "port_map": [
            asdict(check) | {"problem": check.problem} for check in comparison.port_checks
        ],
        "notes": list(comparison.notes),
    }
