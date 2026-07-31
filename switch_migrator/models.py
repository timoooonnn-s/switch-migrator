"""Data model for everything the tool collects and compares."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Platform(str, Enum):
    VOSS = "voss"
    ERS = "ers"


@dataclass
class LldpNeighbor:
    """One LLDP neighbor as seen on a local port.

    sysname is what the neighbor advertises as its system name - often a real
    hostname, but some devices put an adapter model there (Broadcom NICs) or
    leave it empty (HP iLO). ip / sys_descr give a second and third way to
    identify the neighbor when the name is unhelpful.
    """
    sysname: str = ""
    ip: str = ""
    sys_descr: str = ""


@dataclass
class PortState:
    port: str
    description: str = ""
    admin_up: bool | None = None
    oper_up: bool | None = None
    state_reason: str = ""        # VOSS `show int gig state` REASON column (e.g. SSH)
    lldp_neighbor: str = ""       # neighbor SysName (may be empty / an adapter model)
    lldp_neighbor_ip: str = ""    # neighbor management IP, when it advertises one
    lldp_sys_descr: str = ""      # neighbor SysDescr (e.g. 'HPE ProLiant DL380 Gen10')
    is_uplink: bool = False
    # --- migration-sheet fields -------------------------------------------
    uid: str = ""                 # sequential migration ID (P0001), assigned per run
    macs: list[str] = field(default_factory=list)   # learned MACs (capped)
    mac_total: int = 0            # how many were learned in total (before the cap)
    transceiver: str = ""         # pluggable optic type/vendor, when readable
    lacp: bool | None = None      # LACP enabled on the port's MLT
    mlt_id: int | None = None
    mlt_name: str = ""
    tagging: str = ""             # tagged / untagged / mixed / ""
    vlans: list[int] = field(default_factory=list)  # VLANs configured on the port
    isids: list[int] = field(default_factory=list)  # I-SIDs of those VLANs

    @property
    def media(self) -> str:
        """Physical media from the Port Interface DESCRIPTION column
        (10GbSR, Gbic1000BaseT, 40GbCR4, ...)."""
        return self.description


@dataclass
class MltState:
    mlt_id: int
    name: str = ""
    mlt_type: str = ""            # trunk / access / Trunk / Normal ...
    admin: str = ""               # norm / smlt / Enabled / Disabled ...
    current: str = ""             # norm / smlt / ...
    members: list[str] = field(default_factory=list)
    # filled in by cross-referencing port state; None = unknown because the
    # switch gave us no usable port state (do NOT render this as "0 up")
    members_up: int | None = None
    # True/False from the `show mlt` data-path table (LOCAL / LOCAL & REMOTE vs
    # nothing programmed); None = that table was absent. Lets us tell a live
    # MLT from a dead one even when per-port state is unavailable.
    in_datapath: bool | None = None
    is_ist: bool = False
    is_uplink: bool = False

    @property
    def members_total(self) -> int:
        return len(self.members)

    @property
    def members_up_display(self) -> str:
        if self.members_up is None:
            return f"?/{self.members_total}"
        return f"{self.members_up}/{self.members_total}"


@dataclass
class IstState:
    enabled: bool | None = None
    peer_ip: str = ""
    vlan: int | None = None
    session_up: bool | None = None
    raw: str = ""


@dataclass
class VlanInfo:
    vlan_id: int
    name: str = ""                # the VLAN's own name (from 'show vlan basic')
    isid: int | None = None       # local VLAN<->I-SID binding (VOSS only)
    isid_name: str = ""           # the I-SID's name (from 'show vlan i-sid')
    members: list[str] = field(default_factory=list)  # configured port members


@dataclass
class SwitchAudit:
    """Everything collected from one to-be-migrated switch."""
    name: str
    host: str
    platform: Platform
    reachable: bool = False
    ports: list[PortState] = field(default_factory=list)
    mlts: list[MltState] = field(default_factory=list)
    ist: IstState | None = None
    vlans: list[VlanInfo] = field(default_factory=list)
    running_config: str = ""      # raw `show running-config`, only if requested
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ports_up(self) -> int:
        return sum(1 for p in self.ports if p.oper_up)

    @property
    def uplink_ports_up(self) -> int:
        return sum(1 for p in self.ports if p.is_uplink and p.oper_up)

    @property
    def uplink_ports_total(self) -> int:
        return sum(1 for p in self.ports if p.is_uplink)


@dataclass
class FabricIsid:
    """Merged fabric view of one I-SID across all DvR controllers."""
    isid: int
    names: set[str] = field(default_factory=set)
    cvids: set[int] = field(default_factory=set)      # customer VLANs seen attached
    hosts: set[str] = field(default_factory=set)      # BEB host names announcing it
    sources: set[str] = field(default_factory=set)    # config / discover / local
    seen_on: set[str] = field(default_factory=set)    # which DvR reported it


@dataclass
class FabricState:
    """The merged, authoritative state read from all DvR controllers."""
    isids: dict[int, FabricIsid] = field(default_factory=dict)
    dvr_errors: list[str] = field(default_factory=list)
    dvrs_ok: list[str] = field(default_factory=list)

    def get_or_create(self, isid: int) -> FabricIsid:
        if isid not in self.isids:
            self.isids[isid] = FabricIsid(isid=isid)
        return self.isids[isid]

    def isids_for_cvid(self, vlan_id: int) -> list[int]:
        return sorted(i.isid for i in self.isids.values() if vlan_id in i.cvids)

    def merge(self, other: FabricState) -> None:
        """Fold another (per-controller) fabric view into this one."""
        for rec in other.isids.values():
            mine = self.get_or_create(rec.isid)
            mine.names |= rec.names
            mine.cvids |= rec.cvids
            mine.hosts |= rec.hosts
            mine.sources |= rec.sources
            mine.seen_on |= rec.seen_on
        self.dvr_errors.extend(other.dvr_errors)
        self.dvrs_ok.extend(other.dvrs_ok)


class CompStatus(str, Enum):
    OK = "OK"                                    # convention & DvR attachment agree
    OK_NONSTANDARD = "OK_NONSTANDARD"            # found via DvR attachment, but no convention match
    IN_FABRIC_NOT_ATTACHED = "IN_FABRIC_NOT_ATTACHED"  # convention I-SID exists, no c-vid seen on DvR controllers
    AMBIGUOUS = "AMBIGUOUS"                      # multiple candidate I-SIDs match
    MISSING_ON_DVR = "MISSING_ON_DVR"            # nothing found in fabric
    LOCAL_ONLY = "LOCAL_ONLY"                    # VOSS VLAN with no I-SID binding: local L2 by design, not a fabric service
    LOCAL_ISID_NOT_IN_FABRIC = "LOCAL_ISID_NOT_IN_FABRIC"
    LOCAL_BINDING_CONFLICT = "LOCAL_BINDING_CONFLICT"  # switch and DvR disagree
    EXCLUDED = "EXCLUDED"


SEVERITY = {
    CompStatus.OK: "ok",
    CompStatus.OK_NONSTANDARD: "warn",
    CompStatus.IN_FABRIC_NOT_ATTACHED: "warn",
    CompStatus.AMBIGUOUS: "error",
    CompStatus.MISSING_ON_DVR: "error",
    CompStatus.LOCAL_ONLY: "ok",
    CompStatus.LOCAL_ISID_NOT_IN_FABRIC: "error",
    CompStatus.LOCAL_BINDING_CONFLICT: "error",
    CompStatus.EXCLUDED: "ok",
}


@dataclass
class VlanComparison:
    switch: str
    vlan_id: int
    vlan_name: str
    local_isid: int | None            # only for VOSS switches
    expected_isids: list[int]         # from the offset convention / explicit map
    dvr_isids: list[int]              # I-SIDs the DvR controllers show this VLAN attached to
    matched_isid: int | None
    status: CompStatus
    detail: str = ""
    vlan_isid_name: str = ""          # name of the local I-SID (VOSS)

    @property
    def severity(self) -> str:
        return SEVERITY[self.status]
