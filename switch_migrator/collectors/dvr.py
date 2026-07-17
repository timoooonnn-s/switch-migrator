"""Collect and merge the SPB fabric state from the DvR controllers.

Per DvR controller we read:
  * show dvr interfaces        - every DvR interface in the domain with its
                                 L2 I-SID <-> VLAN pair (domain-wide view; no
                                 keyword needed - `l3isid <id>` is only an
                                 optional filter)
  * show isis spbm i-sid all   - every I-SID known fabric-wide (config+discover)
  * show i-sid                 - local I-SIDs incl. c-vid endpoint attachments
  * show vlan i-sid            - local platform-VLAN <-> I-SID bindings

The merged result is the authoritative FabricState the legacy switches are
compared against.
"""

from __future__ import annotations

import logging

from switch_migrator.connection import BaseRunner, CommandError
from switch_migrator.models import FabricState
from switch_migrator.parsers import voss_parsers

log = logging.getLogger(__name__)


def collect_dvr(name: str, runner: BaseRunner, fabric: FabricState) -> None:
    """Merge one DvR controller's view into `fabric`. Thread-unsafe by design:
    give each thread its own FabricState and combine via FabricState.merge().

    A controller only counts as an authoritative source (dvrs_ok) when the
    fabric-wide I-SID list was obtained - comparisons check I-SID *existence*
    against it, and a controller that only delivered its local attachments
    would produce false MISSING_ON_DVR verdicts.
    """
    fabric_wide_ok = False
    partial_ok = False

    try:
        out = runner.run("show dvr interfaces")
        for row in voss_parsers.parse_dvr_interfaces(out):
            rec = fabric.get_or_create(row["l2isid"])
            rec.seen_on.add(name)
            rec.sources.add("dvr")
            rec.cvids.add(row["vlan"])
        partial_ok = True
    except CommandError as exc:
        fabric.dvr_errors.append(f"{name}: {exc}")
        log.error("[%s] %s", name, exc)

    try:
        out = runner.run("show isis spbm i-sid all")
        for row in voss_parsers.parse_isis_spbm_isid(out):
            rec = fabric.get_or_create(row["isid"])
            rec.sources.add(row["type"])
            rec.seen_on.add(name)
            if row["host"]:
                rec.hosts.add(row["host"])
        fabric_wide_ok = True
    except CommandError as exc:
        fabric.dvr_errors.append(f"{name}: {exc}")
        log.error("[%s] %s", name, exc)

    try:
        out = runner.run("show i-sid")
        for isid, info in voss_parsers.parse_isid_local(out).items():
            rec = fabric.get_or_create(isid)
            rec.seen_on.add(name)
            rec.sources.add("local")
            rec.cvids.update(info["cvids"])
            if info["name"]:
                rec.names.add(info["name"])
        partial_ok = True
    except CommandError as exc:
        fabric.dvr_errors.append(f"{name}: {exc}")
        log.error("[%s] %s", name, exc)

    try:
        out = runner.run("show vlan i-sid")
        for vlan in voss_parsers.parse_vlan_isid(out):
            if vlan.isid is None:
                continue
            rec = fabric.get_or_create(vlan.isid)
            rec.seen_on.add(name)
            rec.cvids.add(vlan.vlan_id)
            if vlan.name:
                rec.names.add(vlan.name)
        partial_ok = True
    except CommandError as exc:
        fabric.dvr_errors.append(f"{name}: {exc}")
        log.error("[%s] %s", name, exc)

    if fabric_wide_ok:
        fabric.dvrs_ok.append(name)
    elif partial_ok:
        fabric.dvr_errors.append(
            f"{name}: fabric-wide I-SID list unavailable ('show isis spbm "
            f"i-sid all' failed) - this controller's data is partial and not "
            f"counted as an authoritative fabric source")
