"""Collect the migration-relevant state from one legacy switch (VOSS or ERS)."""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from switch_migrator.config import Config, SwitchTarget
from switch_migrator.connection import CommandError, CommandSource
from switch_migrator.models import (
    LldpNeighbor,
    MltState,
    Platform,
    PortIsidRow,
    PortState,
    SwitchAudit,
    VlanBinding,
    VlanInfo,
    tagging_summary,
)
from switch_migrator.parsers import ers_config, ers_parsers, voss_config, voss_parsers
from switch_migrator.parsers.common import (
    PORT_RE,
    parse_lldp_neighbors,
    parse_lldp_neighbors_summary,
    parse_mac_table,
)
from switch_migrator.usage import (
    classify_audit,
    parse_release_and_model,
    parse_uptime_days,
)

# fallback cap on learned MACs kept per port; the configured value
# (Config.mac_cap) is what the collection actually uses
MAC_CAP = 10

log = logging.getLogger(__name__)

# a per-port VOSS table row: the port, then at least one value
MIN_PORT_ROW_FIELDS = 2


def _is_core_neighbor(sysname: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(sysname.lower(), p.lower()) for p in patterns)


@dataclass(frozen=True)
class Reads:
    """What one switch read includes beyond the core port/MLT/VLAN state."""
    config: bool = False        # `show running-config`: the only tagging source
    macs: bool = False          # MAC tables, optics, counters, uptime
    keep_config: bool = True    # keep the config text; False drops it once read

    @staticmethod
    def for_run(*, extract_config: bool = False, migration_sheets: bool = False,
                verify: bool = False) -> Reads:
        """The usual derivation: the sheets need the running-config for
        tagging, the sheets and the verification need the MACs, and only the
        config extract keeps the config's text."""
        return Reads(config=extract_config or migration_sheets,
                     macs=migration_sheets or verify, keep_config=extract_config)


# the default: the always-needed reads only
NO_EXTRA_READS = Reads()


def collect_switch(target: SwitchTarget, runner: CommandSource, cfg: Config,
                   reads: Reads = NO_EXTRA_READS) -> SwitchAudit:
    audit = SwitchAudit(name=target.name, host=target.host,
                        platform=target.platform, reachable=True)
    # session-setup problems (e.g. paging disable rejected) must be visible
    audit.warnings.extend(getattr(runner, "setup_warnings", []))
    if target.platform is Platform.VOSS:
        _collect_voss(audit, runner)
    else:
        _collect_ers(audit, runner)
    # The running-config feeds --extract-config (VOSS: filter+neutralize; ERS:
    # translate to VOSS flex-UNI) AND is the only source that knows tagged from
    # untagged, so it has to be read before the per-port bindings are built.
    if reads.config:
        out = _run(audit, runner, "show running-config", required=False, absent_ok=True)
        audit.running_config = out or ""
        if not out:
            # absent_ok keeps this out of the warnings, but it is exactly the
            # invisibility the coverage sheet exists to end: without the config
            # the tagging columns are empty, and nothing else would say why
            audit.record_source("running-config", False,
                                "the device returned no running-config")
    # Who this box is and what it runs. One read-only command on every run,
    # not only the ones that pull MACs: a report is read months later, and
    # which release printed the tables behind it is part of what it rests on.
    # It also carries the uptime, which says how long the interface counters
    # have been accumulating ('0 packets' proves nothing on a box that
    # rebooted an hour ago).
    _read_identity(audit, runner)
    _enrich(audit, runner, cfg, pull_macs=reads.macs)
    if not reads.keep_config:
        # The config was read for the tagging it carries, which is now in the
        # bindings. The text itself holds RADIUS keys, SNMP users and the SPB
        # identity, and it would otherwise travel on into the snapshot - a file
        # meant to be handed to a colleague. Only --extract-config, which
        # exists to produce a neutralized version of it, keeps the original.
        audit.running_config = ""
    if reads.macs:
        classify_audit(audit, unused_after_days=cfg.unused_after_days,
                       uptime_days=audit.uptime_days)
    return audit


def _read_identity(audit: SwitchAudit, runner: CommandSource) -> None:
    """Model, release and uptime from `show sys-info`, and a coverage row.

    Recorded as a source either way: a device that does not answer it is a
    device whose report cannot say which release it describes, and that is
    exactly the kind of thinness the coverage sheet exists to show.
    """
    has_capture = getattr(runner, "has_capture", None)
    if has_capture is not None and not has_capture("show sys-info"):
        # replaying a capture taken before every run read sys-info: the
        # device was never asked, so it is not a source that failed - marking
        # it as one turned every Coverage row of an old capture to 'warn'
        log.info("[%s] capture has no 'show sys-info' - release and model "
                 "unknown", audit.name)
        return
    out = _run(audit, runner, "show sys-info", required=False, absent_ok=True)
    if not out:
        audit.record_source("sys-info", False,
                            "the device did not answer 'show sys-info' - the "
                            "release and model of this switch are unknown")
        return
    audit.uptime_days = parse_uptime_days(out)
    audit.release, audit.model = parse_release_and_model(out)
    detail = " ".join(part for part in (audit.model, audit.release) if part)
    audit.record_source("sys-info", True, detail or "no model or release printed")


def _run(audit: SwitchAudit, runner: CommandSource, command: str,
         required: bool, absent_ok: bool = False) -> str | None:
    try:
        return runner.run(command)
    except CommandError as exc:
        # condense the device's answer so the report shows WHY it failed
        detail = " | ".join(
            line.strip() for line in str(exc.output or exc).splitlines()
            if line.strip())[:200]
        # absent_ok: this command legitimately does not exist on every model /
        # release (e.g. 'show ist' on access ERS without an IST, the lldp
        # variant of the other platform, 'show vlan members' on older BOSS).
        # Its failure is expected version variance - log it, but keep it out of
        # the report entirely.
        if absent_ok and not required:
            log.info("[%s] '%s' unavailable on this device (%s) - skipped",
                     audit.name, command, detail)
            return None
        msg = f"'{command}' failed: {detail}"
        (audit.errors if required else audit.warnings).append(msg)
        log.log(logging.ERROR if required else logging.INFO, "[%s] %s", audit.name, msg)
        return None


def _merge_port_details(audit: SwitchAudit, extra: list[PortState]) -> None:
    """Fill gaps in the already-collected ports from a second port table.

    Only ever adds: the first source stays authoritative for the state it
    reported, and ports it never mentioned are appended rather than dropped.
    """
    by_port = {p.port: p for p in audit.ports}
    for other in extra:
        port = by_port.get(other.port)
        if port is None:
            audit.ports.append(other)
            continue
        port.media = port.media or other.media
        if port.admin_up is None:
            port.admin_up = other.admin_up
        if port.oper_up is None:
            port.oper_up = other.oper_up


def _collect_voss(audit: SwitchAudit, runner: CommandSource) -> None:
    """The VOSS reads, in device order."""
    _read_voss_ports(audit, runner)
    out = _run(audit, runner, "show mlt", required=True)
    if out:
        audit.mlts = voss_parsers.parse_mlt(out)
        # LACP admin state comes from the same output - parse it here rather
        # than sending 'show mlt' a second time later
        lacp = voss_parsers.parse_mlt_lacp(out)
        for mlt in audit.mlts:
            mlt.lacp = lacp.get(mlt.mlt_id)
    out = _run(audit, runner, "show virtual-ist", required=False)
    if out:
        audit.ist = voss_parsers.parse_virtual_ist(out)
    _read_voss_vlans(audit, runner)
    # port-level I-SID bindings catch services (e.g. CVLAN/switched-UNI) that
    # `show vlan i-sid` may not list
    out = _run(audit, runner, "show interfaces gigabitEthernet i-sid", required=False)
    if out:
        _merge_port_isids(audit, voss_parsers.parse_port_isid(out))


def _read_voss_ports(audit: SwitchAudit, runner: CommandSource) -> None:
    # Port-state fallback chain: which variants exist differs across 8.x
    # releases. First command that yields ports wins. The plain
    # `show interfaces gigabitEthernet` is deliberately NOT used: it prints
    # several full-width sections (Port Interface, Port Name, Port Config, ...)
    # per port - 2000+ lines on large stacks - which is slow and can desync the
    # session. Both variants below are one narrow row per port and carry the
    # ADMIN/OPER state we actually need.
    port_sources = (
        ("show interfaces gigabitEthernet state", voss_parsers.parse_port_state),
        ("show interfaces gigabitEthernet interface", voss_parsers.parse_ports),
    )
    for command, parser in port_sources:
        # once we already have the port state, a later variant only adds the
        # media type - its absence on this release is not worth a warning
        out = _run(audit, runner, command, required=False,
                   absent_ok=bool(audit.ports))
        if not out:
            continue
        parsed = parser(out)
        if not parsed:
            continue
        if not audit.ports:
            audit.ports = parsed
            # the 'state' table has no DESCRIPTION column, so on its own it
            # leaves the media type blank. Keep going to the 'interface'
            # variant and merge that column in rather than sending a second
            # command later; both tables are one narrow row per port.
            if all(p.media for p in parsed):
                break
            continue
        _merge_port_details(audit, parsed)
        break
    if not audit.ports:
        audit.errors.append(
            "no port state obtained: all 'show interfaces gigabitEthernet' "
            "variants failed or returned nothing")


def _read_voss_vlans(audit: SwitchAudit, runner: CommandSource) -> None:
    out = _run(audit, runner, "show vlan i-sid", required=True)
    if out:
        audit.vlans = voss_parsers.parse_vlan_isid(out)
    out = _run(audit, runner, "show vlan basic", required=False)
    names = voss_parsers.parse_vlan_basic(out) if out else {}
    for vlan in audit.vlans:
        # the VLAN's own name; if 'show vlan basic' didn't cover it, fall back
        # to the I-SID name so the name column is never needlessly blank
        vlan.name = vlan.name or names.get(vlan.vlan_id, "") or vlan.isid_name
    # configured port membership per VLAN (for the inventory report); optional
    # and quiet - not every release has it and it is never load-bearing
    out = _run(audit, runner, "show vlan members", required=False, absent_ok=True)
    if out:
        members = voss_parsers.parse_vlan_members(out)
        for vlan in audit.vlans:
            vlan.members = members.get(vlan.vlan_id, vlan.members)


def _merge_port_isids(audit: SwitchAudit,
                      port_isid_rows: list[PortIsidRow]) -> None:
    # keep them for the per-port VLAN/I-SID columns (flex-UNI boxes have no
    # platform-VLAN members) instead of re-running the command later
    audit.port_isid_rows = port_isid_rows
    by_vlan = {v.vlan_id: v for v in audit.vlans}
    for row in port_isid_rows:
        if row["vlan"] is None:
            continue
        existing = by_vlan.get(row["vlan"])
        if existing is None:
            new = VlanInfo(vlan_id=row["vlan"], isid=row["isid"])
            audit.vlans.append(new)
            by_vlan[row["vlan"]] = new
        elif existing.isid is None:
            existing.isid = row["isid"]
        elif existing.isid != row["isid"]:
            audit.warnings.append(
                f"VLAN {row['vlan']}: port {row['port']} binds I-SID "
                f"{row['isid']} but the VLAN-level binding is {existing.isid}")


def _collect_ers(audit: SwitchAudit, runner: CommandSource) -> None:
    out = _run(audit, runner, "show interfaces", required=True)
    if out:
        audit.ports = ers_parsers.parse_ports(out)
    out = _run(audit, runner, "show mlt", required=True)
    if out:
        audit.mlts = ers_parsers.parse_mlt(out)
    # access ERS boxes dual-homed to an SMLT core have no IST of their own and
    # reject the command outright - that is expected, not a finding
    out = _run(audit, runner, "show ist", required=False, absent_ok=True)
    if out:
        audit.ist = ers_parsers.parse_ist(out)
    out = _run(audit, runner, "show vlan", required=True)
    if out:
        audit.vlans = ers_parsers.parse_vlans(out)


def _collect_fdb(audit: SwitchAudit, runner: CommandSource
                 ) -> dict[str, list[tuple[str, int | None]]]:
    """Learned MAC addresses per port.

    VOSS has no `show mac-address-table`; the forwarding database is read per
    port with `show interfaces gigabitEthernet fdb-entry [<port>]`. The bare
    form (whole box in one command) is tried first; if the release insists on a
    port argument, fall back to asking only the ports that are operationally UP
    - a down port has nothing learned, and this keeps a 48-port box from costing
    48 commands. ERS/BOSS keeps the classic `show mac-address-table`.
    """
    if audit.platform is not Platform.VOSS:
        out = _run(audit, runner, "show mac-address-table",
                   required=False, absent_ok=True)
        return parse_mac_table(out) if out else {}

    out = _run(audit, runner, "show interfaces gigabitEthernet fdb-entry",
               required=False, absent_ok=True)
    if out:
        whole_box = parse_mac_table(out)
        if whole_box:
            return whole_box

    table: dict[str, list[tuple[str, int | None]]] = {}
    up_ports = [p.port for p in audit.ports if p.oper_up]
    if not up_ports:
        return table
    log.info("[%s] per-port FDB fallback for %d up port(s)",
             audit.name, len(up_ports))
    for port in up_ports:
        out = _run(audit, runner,
                   f"show interfaces gigabitEthernet fdb-entry {port}",
                   required=False, absent_ok=True)
        if out:
            for key, entries in parse_mac_table(out).items():
                table.setdefault(key, []).extend(entries)
    return table



def _merge_bindings(target: list[VlanBinding], new: list[VlanBinding]) -> None:
    """Fold one source's bindings into a port's or MLT's list.

    A binding is the same binding when it names the same VLAN (or, for
    untagged traffic that has no c-vid of its own, the same I-SID). Merging
    only ever fills gaps: whichever source spoke first keeps what it said, and
    a later source can add the I-SID or the tagging it did not know. Sources
    are accumulated so the sheet can show where a cell came from.
    """
    for incoming in new:
        match = None
        for existing in target:
            if incoming.vlan is not None and existing.vlan == incoming.vlan:
                match = existing
                break
            if incoming.vlan is None and existing.vlan is None \
                    and existing.isid == incoming.isid:
                match = existing
                break
        if match is None:
            target.append(incoming)
            continue
        if match.isid is None and incoming.isid is not None:
            match.isid, match.isid_note = incoming.isid, ""
        if not match.tagging and incoming.tagging:
            match.tagging = incoming.tagging
        if incoming.source and incoming.source not in match.source.split(","):
            match.source = ",".join(filter(None, [match.source, incoming.source]))


def _sync_flat_lists(holder: PortState | MltState) -> None:
    """Keep the flat vlans/isids lists in step with the bindings.

    Both stay: the pairs are what a human reads, the flat lists are what
    filtering and sorting in the workbook use.
    """
    holder.bindings.sort(key=lambda b: (b.vlan is None, b.vlan or 0, b.isid or 0))
    holder.vlans = sorted({b.vlan for b in holder.bindings if b.vlan is not None})
    holder.isids = sorted({b.isid for b in holder.bindings if b.isid is not None})


# (port -> bindings, MLT id -> bindings, VLAN -> (name, local I-SID or None))
ConfigBindings = tuple[dict[str, list[VlanBinding]], dict[int, list[VlanBinding]],
                       dict[int, tuple[str, "int | None"]]]


def _config_bindings(audit: SwitchAudit,
                     _cache: dict[str, ConfigBindings] | None = None) -> ConfigBindings:
    """Per-port and per-MLT bindings, and the VLANs, from the running-config.

    This is the only source that knows tagged from untagged, so it is read
    first and the show-command sources fill in around it. Returns
    (port bindings, MLT bindings, {vlan_id: (name, local I-SID or None)}).
    """
    if not audit.running_config:
        return {}, {}, {}
    if _cache is not None and "bindings" in _cache:
        return _cache["bindings"]
    try:
        if audit.platform is Platform.VOSS:
            voss = voss_config.parse_voss_config(audit.running_config)
            ports, mlts = voss_config.port_bindings(voss), voss_config.mlt_bindings(voss)
            vlans = {vid: (voss.vlan_names.get(vid, ""), voss.vlan_isid.get(vid))
                     for vid in set(voss.vlan_members) | set(voss.vlan_names)
                     | set(voss.vlan_isid)}
            names = dict(voss.port_names)
        else:
            ers = ers_config.parse_ers_config(audit.running_config)
            ports, mlts = ers_config.port_bindings(ers), {}
            vlans = {vid: (v.name, None) for vid, v in ers.vlans.items()}
            names = {p: port.name for p, port in ers.ports.items() if port.name}
        # the configured port names ride along: the same config, no extra command
        by_port = {p.port: p for p in audit.ports}
        for port_id, name in names.items():
            if port_id in by_port and not by_port[port_id].name:
                by_port[port_id].name = name
    except Exception as exc:                       # never lose a run to a config
        audit.warnings.append(f"running-config not usable as a VLAN source: {exc}")
        audit.record_source("running-config", False, str(exc)[:120])
        # cache the failure as well: with an empty cache the second consumer
        # would parse the config again and add a duplicate warning and a
        # duplicate coverage row
        empty: ConfigBindings = ({}, {}, {})
        if _cache is not None:
            _cache["bindings"] = empty
        return empty
    audit.record_source("running-config", True,
                        f"{len(ports)} port(s) with VLAN bindings")
    result = (ports, mlts, vlans)
    if _cache is not None:
        _cache["bindings"] = result
    return result


def _add_config_only_vlans(audit: SwitchAudit,
                           config_vlans: dict[int, tuple[str, int | None]]) -> None:
    """Adopt VLANs the config configures but `show vlan` never reported.

    A VLAN that exists in the running-config exists on the box. Leaving it out
    of `audit.vlans` would keep it out of the fabric comparison too, and every
    binding for it would then reach the sheet as an unresolved '?'.
    """
    known = {v.vlan_id for v in audit.vlans}
    added = []
    for vid, (name, isid) in sorted(config_vlans.items()):
        if vid in known:
            continue
        audit.vlans.append(VlanInfo(vlan_id=vid, name=name, isid=isid))
        added.append(vid)
    if added:
        log.info("[%s] %d VLAN(s) taken from the running-config that "
                 "'show vlan' did not list: %s", audit.name, len(added),
                 ",".join(map(str, added)))


def _build_port_bindings(audit: SwitchAudit, by_port: dict[str, PortState],
                         cfg_cache: dict[str, ConfigBindings] | None = None) -> None:
    """Assemble each port's VLAN<->I-SID pairs from every source that has them.

    Three sources, most authoritative first:
      running-config   - membership AND tagged/untagged (the only one with it)
      show vlan members - platform-VLAN membership, no tagging
      show interfaces gigabitEthernet i-sid - per-port bindings on a flex-UNI
                         box, where platform VLANs have no members at all
    """
    config_ports, _, config_vlans = _config_bindings(audit, cfg_cache)
    _add_config_only_vlans(audit, config_vlans)
    for port_name, bindings in config_ports.items():
        port = by_port.get(port_name)
        if port is not None:
            _merge_bindings(port.bindings, bindings)

    isid_of = {v.vlan_id: v.isid for v in audit.vlans}
    members_seen = False
    for vlan in audit.vlans:
        for member in vlan.members:
            port = by_port.get(member)
            if port is None:
                continue
            members_seen = True
            _merge_bindings(port.bindings, [VlanBinding(
                vlan=vlan.vlan_id, isid=isid_of.get(vlan.vlan_id),
                source="vlan-members")])
    audit.record_source("vlan membership", members_seen,
                        "" if members_seen else "no VLAN carried member ports")

    for row in audit.port_isid_rows:
        port = by_port.get(row["port"])
        if port is None:
            continue
        _merge_bindings(port.bindings, [VlanBinding(
            vlan=row["vlan"], isid=row["isid"], source="port-i-sid")])

    for port in audit.ports:
        _sync_flat_lists(port)
        port.tagging = tagging_summary(port.bindings)


def _build_mlt_bindings(audit: SwitchAudit, by_port: dict[str, PortState],
                        cfg_cache: dict[str, ConfigBindings] | None = None) -> None:
    """An MLT carries the union of what its member ports carry.

    VOSS prints a VLAN IDS column in `show mlt`; ERS prints nothing at all, so
    reading only that column would leave every ERS aggregation - the uplink MLT
    included - without VLANs on the sheet. Deriving from the members works on
    both platforms, and where `show mlt` does have an opinion the two are
    cross-checked rather than one silently winning.
    """
    _, config_mlts, _cfg_vlans = _config_bindings(audit, cfg_cache)
    isid_of = {v.vlan_id: v.isid for v in audit.vlans}
    for mlt in audit.mlts:
        from_column = list(mlt.vlans)
        mlt.bindings = []
        _merge_bindings(mlt.bindings, config_mlts.get(mlt.mlt_id, []))
        for member in mlt.members:
            port = by_port.get(member)
            if port is None:
                continue
            _merge_bindings(mlt.bindings, [VlanBinding(
                vlan=b.vlan, isid=b.isid, tagging=b.tagging,
                source=f"member {member}" if not b.source else b.source,
                isid_note=b.isid_note) for b in port.bindings])
        # compare BEFORE the column is merged in - afterwards every VLAN it
        # named is in mlt.vlans by construction and the check can never fire
        from_members = {b.vlan for b in mlt.bindings if b.vlan is not None}
        # ...and only when the members had something to say. A box that rejects
        # every 'show interfaces' variant contributes no member bindings at
        # all, and every VLAN in the column would look unaccounted for.
        missing = ([v for v in from_column if v not in from_members]
                   if from_members else [])
        _merge_bindings(mlt.bindings, [
            VlanBinding(vlan=v, isid=isid_of.get(v), source="show mlt")
            for v in from_column])
        _sync_flat_lists(mlt)
        # the column and the members disagreeing is worth knowing about: one of
        # them is describing an aggregation that is not the one on the wire
        if missing:
            audit.warnings.append(
                f"MLT {mlt.mlt_id}: 'show mlt' lists VLAN(s) "
                f"{','.join(map(str, missing))} that no member port carries")


def _enrich_migration_fields(audit: SwitchAudit, runner: CommandSource,
                             pull_macs: bool = False,
                             mac_cap: int = MAC_CAP) -> None:
    """Per-port data the migration sheets need: MLT membership + LACP, VLANs and
    their I-SIDs, tagging, and - when pull_macs is set - learned MACs, the
    pluggable optic and whether the port ever passed traffic. Everything except
    the MAC/optics/counter fetch is derived from data already collected, so it
    costs no extra commands.
    """
    by_port = {p.port: p for p in audit.ports}

    # MLT membership (+ LACP, parsed earlier from the same show mlt output)
    for mlt in audit.mlts:
        for member in mlt.members:
            port = by_port.get(member)
            if port is None:
                continue
            port.mlt_id, port.mlt_name = mlt.mlt_id, mlt.name
            port.lacp = mlt.lacp

    cfg_cache: dict[str, ConfigBindings] = {}
    _build_port_bindings(audit, by_port, cfg_cache)
    _build_mlt_bindings(audit, by_port, cfg_cache)

    if not pull_macs:
        return
    _apply_learned_macs(audit, by_port, _collect_fdb(audit, runner), mac_cap)
    if audit.platform is Platform.VOSS:
        _apply_optics(by_port, _run(audit, runner, "show pluggable-optical-modules basic",
                                    required=False, absent_ok=True))
        _apply_traffic_counters(by_port, _run(
            audit, runner, "show interfaces gigabitEthernet statistics",
            required=False, absent_ok=True))


def _apply_learned_macs(audit: SwitchAudit, by_port: dict[str, PortState],
                        table: dict[str, list[tuple[str, int | None]]],
                        mac_cap: int) -> None:
    """Learned MACs per port, capped; MLT entries fan out to their members."""
    by_mlt = {m.mlt_id: m for m in audit.mlts}
    by_mlt_name = {m.name: m for m in audit.mlts if m.name}
    for key, entries in table.items():
        targets: list[PortState] = []
        mlt = None
        if key.startswith("mlt:"):
            mlt = by_mlt.get(int(key.split(":", 1)[1]))
        elif key.startswith("name:"):
            # real VOSS prints the MLT's NAME in the fdb INTERFACE column
            mlt = by_mlt_name.get(key.split(":", 1)[1])
            if mlt is None:
                log.info("[%s] fdb interface '%s' matches no known MLT - "
                         "entries skipped", audit.name, key[5:])
        if mlt is not None:
            targets = [by_port[m] for m in mlt.members if m in by_port]
        elif key in by_port:
            targets = [by_port[key]]
        for port in targets:
            for mac, _vlan in entries:
                port.mac_total += 1
                if len(port.macs) < mac_cap and mac not in port.macs:
                    port.macs.append(mac)


def _slot_port_rows(output: str | None, by_port: dict[str, PortState]
                    ) -> Iterator[tuple[PortState, list[str]]]:
    """(port, tokens) for every line of a per-port VOSS table that names a known
    slot/port in its first column."""
    for line in (output or "").splitlines():
        tokens = line.split()
        if len(tokens) < MIN_PORT_ROW_FIELDS or not PORT_RE.match(tokens[0]) \
                or "/" not in tokens[0]:
            continue
        port = by_port.get(tokens[0])
        if port is not None:
            yield port, tokens


def _apply_optics(by_port: dict[str, PortState], output: str | None) -> None:
    """Pluggable optics (VOSS). Real columns are
    PORT NUM | TYPE | DDM SUPPORTED | VENDOR NAME | PART NUMBER | SKU, so
    TYPE/VENDOR/PART are taken around the TRUE/FALSE DDM token rather than by
    blind position (releases add/drop trailing columns)."""
    for port, tokens in _slot_port_rows(output, by_port):
        if port.transceiver:
            continue
        ddm = next((i for i, t in enumerate(tokens) if t.upper() in ("TRUE", "FALSE")),
                   None)
        parts = tokens[1:ddm] + tokens[ddm + 1:] if ddm is not None else tokens[1:]
        port.transceiver = " ".join(parts)[:60]


def _apply_traffic_counters(by_port: dict[str, PortState], output: str | None) -> None:
    """Has this port EVER passed traffic? Deliberately layout-independent: the
    classification only needs "are all counters on this row zero?", so no
    assumption is made about which column is packets-in vs octets-out (that
    layout varies by release and no capture pins it)."""
    for port, tokens in _slot_port_rows(output, by_port):
        counters = [int(t) for t in tokens[1:] if t.isdigit()]
        if counters:
            # OR across rows: the output has several sections (packets,
            # errors, ...) and one port appears in each - an all-zero error row
            # must not overwrite a non-zero traffic row
            port.has_traffic = bool(port.has_traffic) or any(c > 0 for c in counters)


def _enrich(audit: SwitchAudit, runner: CommandSource, cfg: Config,
            pull_macs: bool = False) -> None:
    """LLDP neighbor names, uplink flags and MLT member-up counts."""
    neighbors = _read_lldp(audit, runner)
    have_port_state = bool(audit.ports)
    port_oper = {}
    for port in audit.ports:
        n = neighbors.get(port.port)
        if n is not None:
            port.lldp_neighbor = n.sysname
            port.lldp_neighbor_ip = n.ip
            port.lldp_sys_descr = n.sys_descr
        # uplink detection keys on the advertised name (a hostname); adapter
        # models / empty names simply never match the core patterns
        port.is_uplink = bool(port.lldp_neighbor) and _is_core_neighbor(
            port.lldp_neighbor, cfg.core_switch_patterns)
        port_oper[port.port] = bool(port.oper_up)

    for mlt in audit.mlts:
        # Only a real port-state read gives a real up-count. With none, leave
        # members_up = None (unknown) instead of counting every member as down.
        mlt.members_up = (
            sum(1 for m in mlt.members if port_oper.get(m, False))
            if have_port_state else None)
        mlt.is_uplink = any(
            p.is_uplink for p in audit.ports if p.port in mlt.members)

    _enrich_migration_fields(audit, runner, pull_macs=pull_macs,
                             mac_cap=cfg.mac_cap)
    _warn_ist_and_mlts(audit)


def _read_lldp(audit: SwitchAudit, runner: CommandSource) -> dict[str, LldpNeighbor]:
    # Platform-native command first; the other form only as fallback. VOSS has
    # the compact one-line-per-neighbor summary (preferred - stays small on
    # fully-cabled 48-port boxes); BOSS/ERS only knows the block form and
    # answers 'Invalid input' to 'summary'. absent_ok keeps the fallback dance
    # out of the report - only the aggregate warning below matters.
    if audit.platform is Platform.VOSS:
        lldp_sources = (
            ("show lldp neighbor summary", parse_lldp_neighbors_summary),
            ("show lldp neighbor", parse_lldp_neighbors),
        )
    else:
        lldp_sources = (
            ("show lldp neighbor", parse_lldp_neighbors),
            ("show lldp neighbor summary", parse_lldp_neighbors_summary),
        )
    neighbors: dict[str, LldpNeighbor] = {}
    for command, parser in lldp_sources:
        out = _run(audit, runner, command, required=False, absent_ok=True)
        if out:
            neighbors = parser(out)
            if neighbors:
                break
    if not neighbors:
        audit.warnings.append(
            "no LLDP neighbor data - uplink detection disabled for this switch")
    return neighbors


def _warn_ist_and_mlts(audit: SwitchAudit) -> None:
    if audit.ist is not None and audit.ist.session_up is False:
        audit.warnings.append(
            f"IST/vIST session is DOWN (peer {audit.ist.peer_ip or 'unknown'})")
    for mlt in audit.mlts:
        label = f"MLT {mlt.mlt_id} ({mlt.name or 'unnamed'})"
        if not mlt.members:
            audit.warnings.append(
                f"{label}: DEAD MLT - no member ports left; does not need to be "
                f"recreated on the new switch")
        elif mlt.members_up is None:
            # No usable port state. Fall back to the data-path table: NONE means
            # the MLT is genuinely down; LOCAL/REMOTE means it is forwarding.
            # Never emit a bogus '0/N up' here.
            if mlt.in_datapath is False:
                audit.warnings.append(
                    f"{label}: not programmed in the data path - member ports "
                    f"{','.join(mlt.members)} appear DOWN (per-port state "
                    f"unavailable; inferred from 'show mlt')")
        elif mlt.members_up < mlt.members_total:
            audit.warnings.append(
                f"{label}: only {mlt.members_up}/{mlt.members_total} member ports up")
