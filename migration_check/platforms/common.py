"""Value helpers both platform adapters need."""

from __future__ import annotations

import re

_MAC_HEX = re.compile(r"[0-9a-fA-F]")


def normalize_mac(raw: str) -> str | None:
    """'0011.2233.4455', '00-11-22-33-44-55', '00:11:22:33:44:55' -> '00:11:22:33:44:55'.

    The same form fabric-weft produces, so MACs from both platforms compare equal.
    None when the text does not hold exactly twelve hex digits.
    """
    digits = "".join(_MAC_HEX.findall(raw)).lower()
    if len(digits) != 12 or len(raw.strip()) > 17:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def enabled_capabilities(printed: str | None) -> str:
    """LLDP `SysCap` is printed '<supported> / <enabled>' ('rB / rB'); only the enabled
    half says what the neighbour is doing. A value without a slash is taken as is."""
    if not printed:
        return ""
    return printed.rpartition("/")[2].strip() if "/" in printed else printed.strip()
