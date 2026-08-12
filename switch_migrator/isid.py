"""Per-VLAN I-SID resolution for config generation.

With several running offsets (2500000 / 2510000 / 2700000 / 2710000 ...) the
convention alone can't pick an I-SID for a VLAN - `offset + VLAN` gives one
candidate per offset. So resolution is, in order:

  1. excluded            - VLAN is on excluded_vlans / excluded_vlan_names
  2. explicit decision   - isid_conventions.explicit[vlan] (the user's per-VLAN call)
  3. fabric-confirmed    - the audit resolved it against the DvR fabric
  4. review              - none of the above; emit the candidates, decide nothing

Nothing is ever guessed: an unresolved VLAN becomes a REVIEW placeholder, never
an auto-picked I-SID.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from switch_migrator.config import Config


@dataclass
class IsidDecision:
    vlan_id: int
    vlan_name: str
    isid: int | None
    source: str                       # excluded | explicit | fabric | review
    candidates: list[int] = field(default_factory=list)

    @property
    def needs_decision(self) -> bool:
        return self.source == "review"

    @property
    def excluded(self) -> bool:
        return self.source == "excluded"


def candidates_for(vlan_id: int, cfg: Config) -> list[int]:
    return sorted(offset + vlan_id for offset in cfg.isid_offsets)


def resolve(vlan_id: int, vlan_name: str, cfg: Config,
            matched_isid: int | None = None) -> IsidDecision:
    cands = candidates_for(vlan_id, cfg)
    if vlan_id in cfg.excluded_vlans:
        return IsidDecision(vlan_id, vlan_name, None, "excluded", cands)
    if vlan_name:
        for pattern in cfg.excluded_vlan_names:
            if fnmatch.fnmatch(vlan_name.lower(), pattern.lower()):
                return IsidDecision(vlan_id, vlan_name, None, "excluded", cands)
    if vlan_id in cfg.isid_explicit:
        return IsidDecision(vlan_id, vlan_name, cfg.isid_explicit[vlan_id],
                            "explicit", cands)
    if matched_isid is not None:
        return IsidDecision(vlan_id, vlan_name, matched_isid, "fabric", cands)
    return IsidDecision(vlan_id, vlan_name, None, "review", cands)


def build_worksheet(decisions: list[IsidDecision], device_name: str = "") -> str:
    """A human-readable decisions worksheet plus a ready-to-paste config snippet
    for the VLANs that still need a per-VLAN I-SID decision."""
    need = [d for d in decisions if d.needs_decision]
    resolved = [d for d in decisions if not d.needs_decision]
    out: list[str] = [
        "# " + "=" * 74,
        "# I-SID decision worksheet" + (f" - {device_name}" if device_name else ""),
        "# " + "=" * 74,
        f"# {len(resolved)} VLAN(s) resolved, {len(need)} need a decision.",
        "#",
    ]
    if need:
        out.append("# DECISIONS NEEDED - pick one I-SID per VLAN (or exclude it):")
        for d in sorted(need, key=lambda d: d.vlan_id):
            cands = " / ".join(map(str, d.candidates)) or "(no offsets configured)"
            name = f" ({d.vlan_name})" if d.vlan_name else ""
            out.append(f"#   VLAN {d.vlan_id}{name}: candidates {cands}")
        out += [
            "#",
            "# Paste your choices into config.yaml, then re-run:",
            "#",
            "isid_conventions:",
            "  explicit:",
        ]
        for d in sorted(need, key=lambda d: d.vlan_id):
            first = d.candidates[0] if d.candidates else 0
            out.append(f"    {d.vlan_id}: {first}    # candidates: "
                       f"{', '.join(map(str, d.candidates))}")
    else:
        out.append("# No decisions needed - every VLAN resolved.")
    out.append("#")
    out.append("# Resolved:")
    for d in sorted(resolved, key=lambda d: d.vlan_id):
        val = "excluded" if d.excluded else str(d.isid)
        out.append(f"#   VLAN {d.vlan_id}: {val} (from {d.source})")
    return "\n".join(out) + "\n"
