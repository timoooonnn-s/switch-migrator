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
from collections.abc import Callable

from switch_migrator.connection import CommandError, CommandSource
from switch_migrator.models import FabricState
from switch_migrator.parsers import voss_parsers

log = logging.getLogger(__name__)


def collect_dvr(name: str, runner: CommandSource, fabric: FabricState) -> None:
    """Merge one DvR controller's view into `fabric`. Thread-unsafe by design:
    give each thread its own FabricState and combine via FabricState.merge().

    A controller only counts as an authoritative source (dvrs_ok) when the
    fabric-wide I-SID list was obtained - comparisons check I-SID *existence*
    against it, and a controller that only delivered its local attachments
    would produce false MISSING_ON_DVR verdicts.
    """
    fabric_wide_ok = False
    partial_ok = False
    for command, merge in _READS:
        try:
            merge(name, runner.run(command), fabric)
        except CommandError as exc:
            fabric.dvr_errors.append(f"{name}: {exc}")
            log.error("[%s] %s", name, exc)
            continue
        if command == FABRIC_WIDE_READ:
            fabric_wide_ok = True
        else:
            partial_ok = True

    if fabric_wide_ok:
        fabric.dvrs_ok.append(name)
    elif partial_ok:
        fabric.dvr_errors.append(
            f"{name}: fabric-wide I-SID list unavailable ('show isis spbm "
            f"i-sid all' failed) - this controller's data is partial and not "
            f"counted as an authoritative fabric source")


def _merge_dvr_interfaces(name: str, out: str, fabric: FabricState) -> None:
    for row in voss_parsers.parse_dvr_interfaces(out):
        rec = fabric.get_or_create(row["l2isid"])
        rec.seen_on.add(name)
        rec.sources.add("dvr")
        rec.cvids.add(row["vlan"])


def _merge_fabric_isids(name: str, out: str, fabric: FabricState) -> None:
    for announced in voss_parsers.parse_isis_spbm_isid(out):
        rec = fabric.get_or_create(announced["isid"])
        rec.sources.add(announced["type"])
        rec.seen_on.add(name)
        if announced["host"]:
            rec.hosts.add(announced["host"])


def _merge_local_isids(name: str, out: str, fabric: FabricState) -> None:
    for isid, info in voss_parsers.parse_isid_local(out).items():
        rec = fabric.get_or_create(isid)
        rec.seen_on.add(name)
        rec.sources.add("local")
        rec.cvids.update(info["cvids"])
        if info["name"]:
            rec.names.add(info["name"])


def _merge_vlan_isids(name: str, out: str, fabric: FabricState) -> None:
    for vlan in voss_parsers.parse_vlan_isid(out):
        if vlan.isid is None:
            continue
        rec = fabric.get_or_create(vlan.isid)
        rec.seen_on.add(name)
        rec.cvids.add(vlan.vlan_id)
        # the I-SID's name, not the VLAN's: this read is keyed on the I-SID.
        # `VlanInfo.name` is the VLAN's own name and `parse_vlan_isid` only
        # fills it where the release prints a VLAN NAME column, so testing it
        # here silently collected nothing on 8.x.
        if vlan.isid_name:
            rec.names.add(vlan.isid_name)


# the one read that makes a controller authoritative: every I-SID fabric-wide
FABRIC_WIDE_READ = "show isis spbm i-sid all"
# the reads in order, each merged into the fabric by its own function
_READS: tuple[tuple[str, Callable[[str, str, FabricState], None]], ...] = (
    ("show dvr interfaces", _merge_dvr_interfaces),
    (FABRIC_WIDE_READ, _merge_fabric_isids),
    ("show i-sid", _merge_local_isids),
    ("show vlan i-sid", _merge_vlan_isids),
)
