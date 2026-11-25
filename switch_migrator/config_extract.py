"""Extract a neutralized, migration-ready view of a VOSS running-config.

Slice 1 (VOSS -> VOSS): the running-config is already valid VOSS, so we keep
only the port / MLT / VLAN / I-SID banner sections verbatim and drop everything
else. Neutralization is by CONSTRUCTION - device identity and every secret
(mgmt/OOB, SNMP, RADIUS/TACACS, SSH/cert, syslog/NTP, boot flags, and the
SPB/IS-IS core identity: nick-name, system-id, manual-area) live in sections we
never emit, so nothing sensitive can leak. Within the kept sections we annotate
the parts a human must still decide (fabric uplink ports).

Model-agnostic: it keys on the running-config's own section banners, so it works
for a flex-UNI leaf (i-sid/c-vid), a traditional `vlan i-sid` BEB, a BCB (almost
nothing to keep - correct) or an isolated VLAN-only box alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Section-banner names (normalized to UPPER, single-spaced) whose content is the
# port / MLT / VLAN / I-SID configuration we want. Exact match - NOT substring -
# so the many router sections ('OSPF VLAN CONFIGURATION', ...) are never caught.
_KEEP = {
    "PORT CONFIGURATION - PHASE I",
    "PORT CONFIGURATION - PHASE II",
    "PORT CHANNELIZE CONFIGURATION",
    "MLT CONFIGURATION",
    "MLT INTERFACE CONFIGURATION",
    "VLAN CONFIGURATION",
    "I-SID CONFIGURATION",
    "I-SID NAME CONFIGURATION",
}

_IFACE_RE = re.compile(r"^\s*interface\s+(?:GigabitEthernet|mlt)\b", re.IGNORECASE)
_ISIS_RE = re.compile(r"^\s*isis\b", re.IGNORECASE)
# a real banner middle line is "# NAME" (a space after the #); the config's
# "#!end" marker has none, so it is not a section
_BANNER_MID_RE = re.compile(r"^#\s+\S")
# config-mode wrapper commands are structure, not real per-section config
_STRUCTURAL = {"end", "config terminal", "configure terminal", "config t", "enable"}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().upper()


@dataclass
class Section:
    name: str            # "" for the preamble before the first banner
    lines: list[str]

    @property
    def has_content(self) -> bool:
        """True if the section holds real config (not just blanks/comments or
        the config-terminal/end wrappers)."""
        for ln in self.lines:
            s = ln.strip()
            if s and not s.startswith(("#", "!")) and s.lower() not in _STRUCTURAL:
                return True
        return False


def split_sections(text: str) -> list[Section]:
    """Split a VOSS running-config on its `#\\n# NAME\\n#` banner comments."""
    lines = text.splitlines()
    sections: list[Section] = []
    name = ""
    buf: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if (lines[i].strip() == "#" and i + 2 < n
                and lines[i + 2].strip() == "#"
                and _BANNER_MID_RE.match(lines[i + 1])
                and lines[i + 1].strip("# ").strip()):
            sections.append(Section(name, buf))
            name = lines[i + 1].strip("# ").strip()
            buf = []
            i += 3
            continue
        buf.append(lines[i])
        i += 1
    sections.append(Section(name, buf))
    return sections


def _annotate_uplinks(lines: list[str]) -> list[str]:
    """Prefix each fabric-uplink interface block (one that runs IS-IS) with a
    REVIEW comment - those ports are topology-specific on the new device."""
    out: list[str] = []
    block: list[str] | None = None
    for ln in lines:
        if _IFACE_RE.match(ln):
            if block:                       # previous block never saw 'exit'
                out.extend(block)
            block = [ln]
            continue
        if block is not None:
            block.append(ln)
            if ln.strip() == "exit":
                if any(_ISIS_RE.match(x) for x in block):
                    out.append("# [REVIEW] fabric uplink port (runs IS-IS) - "
                               "reconfigure for the new device's topology")
                out.extend(block)
                block = None
            continue
        out.append(ln)
    if block:
        out.extend(block)
    return out


# Lines a ZTP'd new switch already has, or that only make sense on the OLD
# hardware: the B-VLANs (part of the fabric identity ZTP sets up) and taking
# VLAN 1 off the old box's port ranges ('1/1-1/48,2/1-2/6' is that chassis).
_ZTP_OWNED = (
    re.compile(r"^\s*vlan create\s+\d+\s.*\btype\s+spbm-bvlan\b", re.IGNORECASE),
    re.compile(r"^\s*vlan members remove\s+1\s", re.IGNORECASE),
)


def _drop_ztp_owned(lines: list[str]) -> tuple[list[str], list[str]]:
    """(lines to keep, lines left out because ZTP owns them)."""
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        (dropped if any(p.match(line) for p in _ZTP_OWNED) else kept).append(line)
    return kept, dropped


@dataclass
class VossConfigExtract:
    text: str
    kept: list[str]
    omitted_with_content: list[str]
    ztp_owned: list[str] = field(default_factory=list)   # lines left out for ZTP


def _parse_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key, pat in (("model", r"box type\s*:\s*(\S+)"),
                     ("version", r"software version\s*:\s*(\S+)")):
        m = re.search(pat, text)
        if m:
            meta[key] = m.group(1)
    return meta


def _header(device_name: str, meta: dict[str, str], kept: list[str],
            omitted: list[str], ztp_owned: list[str]) -> str:
    bar = "# " + "=" * 74
    out = [bar, "# Neutralized config extract for migration - REVIEW BEFORE USE"]
    if device_name:
        out.append(f"# Source switch : {device_name}")
    if meta.get("model") or meta.get("version"):
        out.append(f"# Source box    : {meta.get('model', '?')} "
                   f"(VOSS {meta.get('version', '?')})")
    out += [
        "# Generated by switch-migrator (read-only). Not for blind paste.",
        "# Apply after review with: enable / configure terminal / <paste> / end",
        "#",
        ("# KEPT verbatim: " + ", ".join(kept)) if kept else "# KEPT: (nothing)",
        "#",
        "# Neutralized by construction - device identity & secrets are NOT here",
        "#   (mgmt/OOB, SNMP, RADIUS/TACACS, SSH/cert, syslog/NTP, boot flags,",
        "#    SPB/IS-IS core identity: nick-name, system-id, manual-area).",
        "#",
        "# Port numbers are the OLD switch's: right for a 1:1 replacement on the",
        "# same model. For a move or a consolidation use --plan, which writes",
        "# config/new/<switch>.cfg on the new port numbers.",
    ]
    if ztp_owned:
        out.append("#")
        out.append("# LEFT OUT - a ZTP'd switch already has these, or they name the old")
        out.append("# hardware's ports:")
        out += [f"#   - {line.strip()}" for line in ztp_owned]
    if omitted:
        out.append("#")
        out.append("# OMITTED but present - configure separately on the new box:")
        out += [f"#   - {name}" for name in omitted]
    out.append(bar)
    out.append("")
    return "\n".join(out) + "\n"


def extract_voss_config(running_config: str,
                        device_name: str = "") -> VossConfigExtract:
    """Neutralized VOSS port/MLT/VLAN/I-SID config extracted from a
    `show running-config`, with a header and uplink REVIEW annotations."""
    sections = split_sections(running_config)
    meta = _parse_meta(running_config)
    kept: list[str] = []
    omitted: list[str] = []
    ztp_owned: list[str] = []
    body: list[str] = []
    for sec in sections:
        if not sec.name:
            continue                         # preamble / device header
        if _norm(sec.name) in _KEEP:
            kept.append(sec.name)
            body += ["#", f"# {sec.name}", "#"]
            lines = sec.lines
            if _norm(sec.name) == "VLAN CONFIGURATION":
                lines, dropped = _drop_ztp_owned(lines)
                ztp_owned += dropped
            if "PORT" in _norm(sec.name):
                lines = _annotate_uplinks(lines)
            body += lines
        elif sec.has_content:
            omitted.append(sec.name)
    text = (_header(device_name, meta, kept, omitted, ztp_owned)
            + "\n".join(body).rstrip() + "\n")
    return VossConfigExtract(text=text, kept=kept, omitted_with_content=omitted,
                             ztp_owned=ztp_owned)
