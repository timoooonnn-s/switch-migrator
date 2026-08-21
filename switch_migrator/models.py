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


# How a VLAN egresses a port. The distinction is what you actually configure
# on the new switch - `c-vid <vlan> port ...` for tagged, `untagged-traffic
# port ...` for untagged - so it matters more than a bare PVID number, which
# a flex-UNI leaf does not really have.
TAGGED = "t"
UNTAGGED = "u"
BOTH = "t+u"          # VOSS: a c-vid AND untagged-traffic on the same I-SID


# How a missing I-SID is spelled on the sheet. '?' (the default) means the
# tool looked and did not find one - a task, not a blank.
_ISID_NOTES = {"local": "(local)", "excluded": "(excluded)"}


@dataclass
class VlanBinding:
    """One VLAN carried on a port or MLT, with its I-SID and how it egresses.

    Kept as a pair rather than two parallel lists because the sheets used to
    print 'VLANs 200,4000' beside 'I-SIDs 10200' and leave the reader to guess
    which was which - and to silently drop any VLAN whose I-SID was unknown.
    `isid is None` is therefore a real state ('local VLAN' or 'not resolved'),
    distinguished by `isid_note`, never an omission.
    """
    vlan: int | None = None       # None = untagged traffic with no c-vid of its own
    isid: int | None = None
    tagging: str = ""             # TAGGED / UNTAGGED / BOTH, '' when unknown
    source: str = ""              # which collected source this came from
    isid_note: str = ""           # 'local' / 'unresolved' when isid is None

    @property
    def key(self) -> tuple[int | None, int | None]:
        return (self.vlan, self.isid)

    def render(self) -> str:
        """'200->10200 (t)' - one self-contained cell entry."""
        left = str(self.vlan) if self.vlan is not None else "untagged"
        right = (str(self.isid) if self.isid is not None
                 else _ISID_NOTES.get(self.isid_note, "?"))
        text = f"{left}->{right}"
        return f"{text} ({self.tagging})" if self.tagging else text


def tagging_summary(bindings: list[VlanBinding]) -> str:
    """One word for a whole port or MLT, derived from its bindings.

    Only says something when the bindings do. The rule this replaced - one
    VLAN means untagged, several mean tagged - called a trunk carrying a
    single tagged VLAN 'untagged', which is a wrong port on the new switch.
    """
    kinds = {b.tagging for b in bindings if b.tagging}
    if not kinds:
        return ""
    if kinds == {TAGGED}:
        return "tagged"
    if kinds == {UNTAGGED}:
        return "untagged"
    return "mixed"


@dataclass
class SourceStatus:
    """Whether one collected source answered, for the coverage report.

    A command that a release rejects is not an error - but it does mean a
    column is thinner than it looks, and that has to be visible somewhere.
    """
    name: str                     # the command, or a derived source's name
    ok: bool
    detail: str = ""


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
    last_change: str = ""         # when the port last changed state (DATE column)
    last_change_days: int | None = None   # age of that change, in days
    has_traffic: bool | None = None       # any non-zero counter; None = unknown
    usage: str = ""               # IN USE / DEGRADED / UNCERTAIN / LIKELY UNUSED / UNUSED
    usage_evidence: str = ""      # why the tool decided that
    macs: list[str] = field(default_factory=list)   # learned MACs (capped)
    mac_total: int = 0            # how many were learned in total (before the cap)
    transceiver: str = ""         # pluggable optic type/vendor, when readable
    lacp: bool | None = None      # LACP enabled on the port's MLT
    mlt_id: int | None = None
    mlt_name: str = ""
    tagging: str = ""             # tagged / untagged / mixed / ""
    vlans: list[int] = field(default_factory=list)  # VLANs configured on the port
    isids: list[int] = field(default_factory=list)  # I-SIDs of those VLANs
    # the authoritative form of the two lists above: VLAN<->I-SID pairs that
    # also carry tagged/untagged. The flat lists stay for filtering and sorting.
    bindings: list[VlanBinding] = field(default_factory=list)

    @property
    def binding_sources(self) -> str:
        """Which sources the VLAN/I-SID cell was built from (provenance)."""
        return ",".join(sorted({part for b in self.bindings
                                for part in b.source.split(",") if part}))

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
    lacp: bool | None = None      # LACP admin state (VOSS: show mlt LACP table)
    vlans: list[int] = field(default_factory=list)  # VLAN IDS column of show mlt
    isids: list[int] = field(default_factory=list)  # I-SIDs of those VLANs
    bindings: list[VlanBinding] = field(default_factory=list)
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
    uptime_days: int | None = None  # how long counters have accumulated
    # rows from `show interfaces gigabitEthernet i-sid`, kept so the per-port
    # VLAN/I-SID columns need no second call
    port_isid_rows: list[dict] = field(default_factory=list)
    # which sources answered on this device, for the coverage report
    sources: list[SourceStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record_source(self, name: str, ok: bool, detail: str = "") -> None:
        self.sources.append(SourceStatus(name=name, ok=ok, detail=detail))

    @property
    def ports_with_vlans(self) -> int:
        return sum(1 for p in self.ports if p.bindings)

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
