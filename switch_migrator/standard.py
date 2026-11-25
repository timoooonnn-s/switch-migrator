"""The estate's naming and numbering conventions - the company standard.

Copied from the `port-preparer` experiment (council decision D4, revised: copy, do
not depend). Two sources of truth disagree on purpose:

* the RULES are what new objects must follow - the `*_for_new_*` functions;
* the estate is older than its rules, so existing objects that break them are
  LEGACY, not wrong - the `classify_*` / `is_legacy_*` functions.

Confusing the two is how a tool ends up calling 300 switches broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# a convention I-SID: three prefix digits, four VLAN digits
ISID_DIGITS = 7
# a slot/port id
SLOT_PORT_PARTS = 2
# 4xx encodes the port in two digits
MAX_MLT_PORT = 99


class ConventionError(ValueError):
    """The value cannot be expressed under the estate's conventions."""


class Environment(StrEnum):
    PROD = "prod"
    NONPROD = "nonprod"
    SPECIAL = "special"
    INFRA = "infra"          # vIST, B-VLANs: reserved, never generated


# --- I-SID numbering -----------------------------------------------------------
#
# `<prefix><VLAN zero-padded to 4>`. The prefix encodes the environment. Within an
# environment the two variants are equivalent ("the engineer's mood"); new
# services get the lower one.

ISID_PREFIX_CANONICAL: dict[Environment, int] = {
    Environment.PROD: 250,
    Environment.NONPROD: 270,
    Environment.SPECIAL: 299,
}
ISID_PREFIX_ACCEPTED: dict[int, Environment] = {
    250: Environment.PROD, 251: Environment.PROD,
    270: Environment.NONPROD, 271: Environment.NONPROD,
    299: Environment.SPECIAL,
}
# infrastructure I-SIDs (1531100 on the vIST VLAN) follow their own scheme and
# are never generated
KNOWN_INFRA_PREFIXES: set[int] = {153}

MIN_VLAN_ID = 2
MAX_VLAN_ID = 4084
# VLAN 31 is the inter-switch trunk VLAN, estate-wide: never allocated, never touched
RESERVED_VLAN_IDS: set[int] = {1, 31}


def isid_for_new_service(vlan_id: int, environment: Environment) -> int:
    """The I-SID a NEW service on `vlan_id` gets: 695 in prod -> 2500695."""
    if environment not in ISID_PREFIX_CANONICAL:
        raise ConventionError(f"no I-SID prefix for environment {environment.value!r}; "
                              f"infrastructure I-SIDs are never generated")
    if not MIN_VLAN_ID <= vlan_id <= MAX_VLAN_ID:
        raise ConventionError(f"VLAN {vlan_id} is outside {MIN_VLAN_ID}-{MAX_VLAN_ID}")
    if vlan_id in RESERVED_VLAN_IDS:
        raise ConventionError(f"VLAN {vlan_id} is reserved estate-wide")
    return int(f"{ISID_PREFIX_CANONICAL[environment]}{vlan_id:04d}")


@dataclass(frozen=True)
class IsidClassification:
    isid: int
    prefix: int
    environment: Environment | None
    vlan_id: int | None
    canonical: bool
    note: str = ""


def classify_isid(isid: int) -> IsidClassification:
    """Take an existing I-SID apart. Unknown shapes are described, not rejected."""
    text = str(isid)
    if len(text) != ISID_DIGITS:
        return IsidClassification(isid, 0, None, None, False,
                                  f"not 7 digits, cannot decompose ({text})")
    prefix, tail = int(text[:3]), int(text[3:])
    if prefix in KNOWN_INFRA_PREFIXES:
        return IsidClassification(isid, prefix, Environment.INFRA, None, False,
                                  "infrastructure prefix; follows its own scheme")
    environment = ISID_PREFIX_ACCEPTED.get(prefix)
    if environment is None:
        return IsidClassification(isid, prefix, None, tail, False,
                                  f"unrecognised prefix {prefix}")
    canonical = prefix == ISID_PREFIX_CANONICAL[environment]
    note = "" if canonical else (f"legacy variant of the {environment.value} prefix "
                                 f"(canonical is {ISID_PREFIX_CANONICAL[environment]})")
    return IsidClassification(isid, prefix, environment, tail, canonical, note)


def isid_matches_vlan(isid: int, vlan_id: int) -> bool:
    """True if `isid` encodes `vlan_id` under any accepted prefix."""
    decoded = classify_isid(isid)
    return decoded.vlan_id == vlan_id and decoded.environment in ISID_PREFIX_CANONICAL


# --- MLT numbering and naming --------------------------------------------------
#
# NEW MLTs are 4xx, where xx is the number of the FIRST member port: a LAG on
# 1/10 (and 1/11) is MLT 410. The LACP key is the MLT id. Ids outside the block
# predate the rule and are legacy.

MLT_NEW_BASE = 400
MLT_ID_MAX = 512
# the default MLT name, as observed in production: f110 = server LAG on 1/10.
# Changed with `plan.mlt_name` in config.yaml.
DEFAULT_MLT_NAME = "f{slot}{port}"


def _slot_port(port_id: str) -> tuple[int, int]:
    parts = port_id.strip().split("/")
    if len(parts) != SLOT_PORT_PARTS:
        raise ConventionError(
            f"the 4xx MLT rule is only defined for slot/port ids; {port_id!r} "
            f"needs an explicit MLT id")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ConventionError(f"cannot read slot/port out of {port_id!r}") from None


def mlt_id_for_new_lag(first_port: str) -> int:
    """MLT id for a NEW LAG whose first member is `first_port`: 1/10 -> 410."""
    _, port = _slot_port(first_port)
    if not 1 <= port <= MAX_MLT_PORT:
        raise ConventionError(f"the 4xx rule only encodes ports 1-99; port {port} "
                              f"needs an explicit MLT id")
    return MLT_NEW_BASE + port


def mlt_name_for_new_lag(first_port: str, template: str = DEFAULT_MLT_NAME) -> str:
    """The NEW LAG's name from the template. Fields: {slot}, {port}, {id}."""
    slot, port = _slot_port(first_port)
    try:
        return template.format(slot=slot, port=port, id=mlt_id_for_new_lag(first_port))
    except (KeyError, IndexError, ValueError) as exc:
        raise ConventionError(f"MLT name template {template!r} is not usable: {exc}") from None


def is_legacy_mlt_id(mlt_id: int) -> bool:
    """An id outside the 4xx block: it predates the rule."""
    return not (MLT_NEW_BASE < mlt_id <= MLT_NEW_BASE + 99)


def lacp_key_for(mlt_id: int) -> int:
    """The LACP key is the MLT id (production: `mlt 197` / `lacp enable key 197`)."""
    return mlt_id
