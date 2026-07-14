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
    """Merge one DvR controller's view into `fabric`. Thread-unsafe by design: call it
    from a single thread or lock around it (the CLI merges sequentially)."""
    ok = False

    try:
        out = runner.run("show dvr interfaces")
        for row in voss_parsers.parse_dvr_interfaces(out):
            rec = fabric.get_or_create(row["l2isid"])
            rec.seen_on.add(name)
            rec.sources.add("dvr")
            rec.cvids.add(row["vlan"])
        ok = True
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
        ok = True
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
        ok = True
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
        ok = True
    except CommandError as exc:
        fabric.dvr_errors.append(f"{name}: {exc}")
        log.error("[%s] %s", name, exc)

    if ok:
        fabric.dvrs_ok.append(name)
