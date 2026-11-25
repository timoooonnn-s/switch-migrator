"""Comparison engine: legacy switch VLANs vs. authoritative DvR fabric state.

Matching strategy (per VLAN on a to-be-migrated switch):

1. Expected I-SIDs from the convention: explicit per-VLAN mapping if present,
   otherwise every configured offset + VLAN-ID is a candidate.
2. Actual I-SIDs from the fabric: every I-SID any DvR controller shows this VLAN (c-vid)
   attached to.
3. On VOSS switches the switch's own local VLAN<->I-SID binding is checked as
   well - it must exist in the fabric and should match the convention.

The DvR state is authoritative: a match found via DvR attachment always wins,
convention disagreement is only a warning.
"""

from __future__ import annotations

import fnmatch

from switch_migrator.config import Config
from switch_migrator.models import (
    CompStatus,
    FabricState,
    MltState,
    Platform,
    PortState,
    SwitchAudit,
    VlanComparison,
    VlanInfo,
)


def expected_isids(vlan_id: int, cfg: Config) -> list[int]:
    if vlan_id in cfg.isid_explicit:
        return [cfg.isid_explicit[vlan_id]]
    return sorted(offset + vlan_id for offset in cfg.isid_offsets)


def compare_switch(audit: SwitchAudit, fabric: FabricState,
                   cfg: Config) -> list[VlanComparison]:
    results = []
    for vlan in audit.vlans:
        results.append(_compare_vlan(audit, vlan, fabric, cfg))
    return results


# (status, matched I-SID, detail) - what one rule concludes about a VLAN
Verdict = tuple[CompStatus, int | None, str]


def _compare_vlan(audit: SwitchAudit, vlan: VlanInfo, fabric: FabricState,
                  cfg: Config) -> VlanComparison:
    expected = expected_isids(vlan.vlan_id, cfg)
    dvr_attached = fabric.isids_for_cvid(vlan.vlan_id)
    # VLAN-only (ERS, or VOSS VLAN without I-SID): the mapping is derived
    convention_hits = [i for i in expected if i in fabric.isids]
    # the rules in order; the first that concludes anything decides
    status, matched, detail = (
        _judge_excluded(audit.name, vlan, cfg)
        or _judge_local_binding(audit, vlan, fabric, expected, dvr_attached)
        or _judge_attached(vlan.vlan_id, expected, dvr_attached, convention_hits)
        or _judge_convention_only(vlan.vlan_id, convention_hits)
        or _judge_absent(audit, vlan, expected))
    return VlanComparison(
        switch=audit.name, vlan_id=vlan.vlan_id, vlan_name=vlan.name,
        vlan_isid_name=vlan.isid_name, local_isid=vlan.isid,
        expected_isids=expected, dvr_isids=dvr_attached, matched_isid=matched,
        status=status, detail=detail,
    )


def _judge_excluded(switch: str, vlan: VlanInfo, cfg: Config) -> Verdict | None:
    excluded_by = None
    if vlan.vlan_id in cfg.excluded_vlans:
        excluded_by = f"VLAN id {vlan.vlan_id} is on the excluded_vlans list"
    elif vlan.name:
        for pattern in cfg.excluded_vlan_names:
            if fnmatch.fnmatch(vlan.name.lower(), pattern.lower()):
                excluded_by = (f"VLAN name '{vlan.name}' matches excluded "
                               f"pattern '{pattern}'")
                break
    if not excluded_by:
        return None
    return (CompStatus.EXCLUDED, None,
            f"{excluded_by} - exists on {switch}, intentionally "
            f"absent from the fabric")


def _judge_local_binding(audit: SwitchAudit, vlan: VlanInfo, fabric: FabricState,
                         expected: list[int], dvr_attached: list[int]) -> Verdict | None:
    """A VOSS switch with its own local binding: that binding is the claim to verify."""
    local_isid = vlan.isid
    if audit.platform is not Platform.VOSS or local_isid is None:
        return None
    if local_isid not in fabric.isids:
        return (CompStatus.LOCAL_ISID_NOT_IN_FABRIC, None,
                f"switch binds VLAN {vlan.vlan_id} to I-SID {local_isid}, "
                f"but no DvR controller knows that I-SID")
    if dvr_attached and local_isid not in dvr_attached:
        return (CompStatus.LOCAL_BINDING_CONFLICT, None,
                f"switch binds VLAN {vlan.vlan_id} to I-SID {local_isid}, "
                f"but the DvR controllers attach that VLAN to "
                f"{dvr_attached} - resolve before migrating")
    if local_isid in expected:
        return (CompStatus.OK, local_isid,
                "local binding matches convention and exists in fabric")
    return (CompStatus.OK_NONSTANDARD, local_isid,
            f"I-SID {local_isid} exists in fabric but does not match the "
            f"convention (expected one of {expected})")


def _judge_attached(vlan_id: int, expected: list[int], dvr_attached: list[int],
                    convention_hits: list[int]) -> Verdict | None:
    """The DvR controllers attach the VLAN: they are authoritative."""
    agreed = sorted(set(convention_hits) & set(dvr_attached))
    if len(agreed) > 1:
        return (CompStatus.AMBIGUOUS, None,
                f"multiple convention I-SIDs carry VLAN {vlan_id} on the "
                f"DvR controllers: {agreed} - resolve via isid_conventions.explicit")
    if agreed:
        return (CompStatus.OK, agreed[0],
                "convention I-SID confirmed by DvR VLAN attachment")
    if len(dvr_attached) > 1:
        return (CompStatus.AMBIGUOUS, None,
                f"DvR controllers show VLAN {vlan_id} attached to multiple I-SIDs "
                f"{dvr_attached}, none matching the convention")
    if dvr_attached:
        return (CompStatus.OK_NONSTANDARD, dvr_attached[0],
                f"DvR controllers attach VLAN {vlan_id} to I-SID {dvr_attached[0]}, "
                f"which does not match the convention (expected {expected})")
    return None


def _judge_convention_only(vlan_id: int, convention_hits: list[int]) -> Verdict | None:
    """Convention I-SIDs exist in the fabric, but nothing attaches the VLAN."""
    if len(convention_hits) == 1:
        return (CompStatus.IN_FABRIC_NOT_ATTACHED, convention_hits[0],
                f"I-SID {convention_hits[0]} exists in the fabric but no DvR controller "
                f"shows VLAN {vlan_id} attached - verify the c-vid on the "
                f"terminating BEBs before migrating")
    if convention_hits:
        return (CompStatus.AMBIGUOUS, None,
                f"multiple convention candidates exist in the fabric "
                f"({convention_hits}) and no DvR-controller attachment disambiguates "
                f"them - resolve via isid_conventions.explicit")
    return None


def _judge_absent(audit: SwitchAudit, vlan: VlanInfo, expected: list[int]) -> Verdict:
    """Nothing anywhere. On a VOSS switch a VLAN that carries no local I-SID
    binding is a local-only L2 VLAN by design (quarantine, default, B-VLANs,
    management) - it was never extended over the fabric and is not supposed to
    be. Flagging it as a missing fabric service is a false positive; report it
    as LOCAL_ONLY instead. ERS VLANs keep MISSING_ON_DVR: they have no I-SID
    concept at all, so absence from the fabric IS the actionable finding."""
    if audit.platform is Platform.VOSS and vlan.isid is None:
        return (CompStatus.LOCAL_ONLY, None,
                f"VLAN {vlan.vlan_id} has no I-SID binding on {audit.name} and is "
                f"absent from the fabric - local-only L2 VLAN, recreate it "
                f"locally on the new switch (no fabric service required)")
    return (CompStatus.MISSING_ON_DVR, None,
            f"no candidate I-SID (checked {expected}) exists in the fabric "
            f"and no DvR controller attaches VLAN {vlan.vlan_id} - service must be created "
            f"before migration")


def resolve_binding_isids(audits: list[SwitchAudit],
                          comparisons: dict[str, list[VlanComparison]],
                          fabric_checked: bool = True) -> None:
    """Fill in the I-SID of every port/MLT binding the device could not name.

    With `fabric_checked` false (--no-fabric) nothing is resolved and the
    unresolved bindings say so, rather than claiming a failed lookup.

    A VOSS switch states its own VLAN<->I-SID bindings, so its ports mostly
    arrive with I-SIDs already. An ERS switch has no I-SIDs at all: without
    this step every ERS port and every ERS MLT reaches the cabling sheet with
    an empty I-SID column, on the very platform being migrated away from.

    The comparison already resolved each VLAN against the fabric, so the answer
    exists - it just never reached the sheets. Where it resolved to nothing,
    the binding says why (`local`, `excluded`) instead of going blank, because
    a blank cell reads as 'no I-SID here' when it means 'we did not find one'.
    """
    notes = {
        CompStatus.LOCAL_ONLY: "local",
        CompStatus.EXCLUDED: "excluded",
    }
    # in --no-fabric mode there is no fabric to resolve against, and saying '?'
    # would claim the tool looked and found nothing - on an isolated estate
    # that is every VLAN on the sheet flagged for no reason
    unmatched = "unresolved" if fabric_checked else "no-fabric"
    for audit in audits:
        by_vlan = {c.vlan_id: c for c in comparisons.get(audit.name, [])}
        holders: list[PortState | MltState] = [*audit.ports, *audit.mlts]
        for holder in holders:
            for binding in holder.bindings:
                if binding.vlan is None or binding.isid is not None:
                    continue
                comp = by_vlan.get(binding.vlan)
                if comp is not None and comp.matched_isid is not None:
                    binding.isid = comp.matched_isid
                    binding.isid_note = ""
                    binding.source = ",".join(
                        filter(None, [binding.source, "fabric"]))
                elif comp is not None:
                    binding.isid_note = notes.get(comp.status, unmatched)
                else:
                    binding.isid_note = unmatched
            holder.isids = sorted({b.isid for b in holder.bindings
                                   if b.isid is not None})
