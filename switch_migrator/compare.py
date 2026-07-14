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

from switch_migrator.config import Config
from switch_migrator.models import (
    CompStatus,
    FabricState,
    Platform,
    SwitchAudit,
    VlanComparison,
)


def expected_isids(vlan_id: int, cfg: Config) -> list[int]:
    if vlan_id in cfg.isid_explicit:
        return [cfg.isid_explicit[vlan_id]]
    return sorted(offset + vlan_id for offset in cfg.isid_offsets)


def compare_switch(audit: SwitchAudit, fabric: FabricState,
                   cfg: Config) -> list[VlanComparison]:
    results = []
    for vlan in audit.vlans:
        results.append(_compare_vlan(audit, vlan.vlan_id, vlan.name,
                                     vlan.isid, fabric, cfg))
    return results


def _compare_vlan(audit: SwitchAudit, vlan_id: int, vlan_name: str,
                  local_isid: int | None, fabric: FabricState,
                  cfg: Config) -> VlanComparison:
    expected = expected_isids(vlan_id, cfg)
    dvr_attached = fabric.isids_for_cvid(vlan_id)

    def result(status: CompStatus, matched: int | None, detail: str) -> VlanComparison:
        return VlanComparison(
            switch=audit.name, vlan_id=vlan_id, vlan_name=vlan_name,
            local_isid=local_isid, expected_isids=expected,
            dvr_isids=dvr_attached, matched_isid=matched,
            status=status, detail=detail,
        )

    if vlan_id in cfg.excluded_vlans:
        return result(CompStatus.EXCLUDED, None, "VLAN excluded via config")

    # --- VOSS switch with its own local binding: that binding is the claim to verify
    if audit.platform is Platform.VOSS and local_isid is not None:
        if local_isid not in fabric.isids:
            return result(CompStatus.LOCAL_ISID_NOT_IN_FABRIC, None,
                          f"switch binds VLAN {vlan_id} to I-SID {local_isid}, "
                          f"but no DvR controller knows that I-SID")
        if local_isid in expected:
            return result(CompStatus.OK, local_isid,
                          "local binding matches convention and exists in fabric")
        return result(CompStatus.OK_NONSTANDARD, local_isid,
                      f"I-SID {local_isid} exists in fabric but does not match the "
                      f"convention (expected one of {expected})")

    # --- VLAN-only (ERS, or VOSS VLAN without I-SID): derive the mapping
    convention_hits = [i for i in expected if i in fabric.isids]
    agreed = sorted(set(convention_hits) & set(dvr_attached))

    if agreed:
        if len(agreed) > 1:
            return result(CompStatus.AMBIGUOUS, None,
                          f"multiple convention I-SIDs carry VLAN {vlan_id} on the "
                          f"DvR controllers: {agreed} - resolve via isid_conventions.explicit")
        return result(CompStatus.OK, agreed[0],
                      "convention I-SID confirmed by DvR VLAN attachment")

    if dvr_attached:
        if len(dvr_attached) > 1:
            return result(CompStatus.AMBIGUOUS, None,
                          f"DvR controllers show VLAN {vlan_id} attached to multiple I-SIDs "
                          f"{dvr_attached}, none matching the convention")
        return result(CompStatus.OK_NONSTANDARD, dvr_attached[0],
                      f"DvR controllers attach VLAN {vlan_id} to I-SID {dvr_attached[0]}, "
                      f"which does not match the convention (expected {expected})")

    if len(convention_hits) == 1:
        return result(CompStatus.IN_FABRIC_NOT_ATTACHED, convention_hits[0],
                      f"I-SID {convention_hits[0]} exists in the fabric but no DvR controller "
                      f"shows VLAN {vlan_id} attached - verify the c-vid on the "
                      f"terminating BEBs before migrating")
    if len(convention_hits) > 1:
        return result(CompStatus.AMBIGUOUS, None,
                      f"multiple convention candidates exist in the fabric "
                      f"({convention_hits}) and no DvR-controller attachment disambiguates "
                      f"them - resolve via isid_conventions.explicit")

    return result(CompStatus.MISSING_ON_DVR, None,
                  f"no candidate I-SID (checked {expected}) exists in the fabric "
                  f"and no DvR controller attaches VLAN {vlan_id} - service must be created "
                  f"before migration")
