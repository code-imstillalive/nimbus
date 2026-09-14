"""Resolve a NEM region and a coarse postcode prefix from a reverse-
geocoded address string.

nimbus issue #495 (Signals 6/7 of #489) needs two fields that did not
exist anywhere in Nimbus, both in the telemetry schema's top-level
`required` array:

    "region":          { "enum": ["NSW1","QLD1","VIC1","SA1","TAS1"] }
    "postcode_prefix": { "pattern": "^[0-9]{3}$",
                         "description": "First 3 digits of the household
                         postcode. Privacy boundary." }

Mark Purcell's own answer to how a household should supply them, after
checking what is already installed rather than proposing a new
mechanism: *"Use companion app reverse geocode as a one time setup."*

Home Assistant's Companion App already publishes
`sensor.<device>_geocoded_location`, whose state is a full formatted
address ending `..., <STATE> <POSTCODE>, Australia`. That is HA's own
integration doing the reverse-geocode; Nimbus makes no outbound call of
its own, and reads an already-installed entity exactly like every other
external-fact reader in this codebase.

**Why `region` is parsed from the address and not from a sensor's own
entity_id.** It looks derivable from the configured spot-price sensor
(`sensor.aemo_nem_qld1_...` -> `QLD1`), and an earlier triage on #495
proposed exactly that. `const.py`'s own comments around
`CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR` record that "region's entity
name hardcoding" was already a real bug this project fixed once. Parsing
the state's own address is not a second instance of that: it reads a
documented value out of a documented field, rather than inferring
meaning from a name a third-party integration happens to have chosen.

**"One time" is the caller's job, not this module's.** These are pure
functions over a string. Mark's own caveat on the issue is that the
Companion App tracks wherever the registered phone currently is, not the
fixed installation address -- fine as a coarse region proxy on a normal
day, but not something to re-derive on every solve. The caller resolves
once and stores the result.
"""

from __future__ import annotations

import re

# Australian state/territory abbreviation -> NEM region id.
#
# ACT maps to NSW1 deliberately and is the one non-obvious entry: the
# ACT has no NEM region of its own, it sits inside NSW1. Getting this
# wrong would silently mis-tag every record from a Canberra household.
#
# WA and NT are absent just as deliberately -- they are not in the NEM
# at all (SWIS and the NT network are separate systems), so there is no
# honest region to return. A household there gets None rather than a
# nearest-guess, because a wrong region id is worse than a missing one
# in a telemetry feed keyed on it.
_STATE_TO_NEM_REGION: dict[str, str] = {
    "NSW": "NSW1",
    "ACT": "NSW1",
    "QLD": "QLD1",
    "VIC": "VIC1",
    "SA": "SA1",
    "TAS": "TAS1",
}

# The trailing "<STATE> <POSTCODE>" pair in a formatted Australian
# address, e.g. "12 Example St, Noosa Heads QLD 4567, Australia".
#
# Anchored on the state abbreviation followed by exactly four digits,
# searched anywhere in the string rather than at a fixed offset -- the
# Companion App's formatted address varies in how many leading
# components it includes (unit/street/suburb), and a positional parse
# would break on the first address shaped differently from the one it
# was written against.
_STATE_POSTCODE_RE = re.compile(
    r"\b(NSW|ACT|QLD|VIC|SA|TAS|WA|NT)\s+(\d{4})\b",
    re.IGNORECASE,
)


def parse_state_and_postcode(address: str | None) -> tuple[str | None, str | None]:
    """The raw `(STATE, POSTCODE)` pair from a formatted address, or
    `(None, None)` when it carries no recognisable pair.

    Returns the state uppercased and the postcode as its original
    4-digit string. Deliberately does not map to a NEM region here --
    `nem_region_for_state()` owns that, so a caller that only wants the
    postcode is not forced through a NEM-specific lookup that can
    legitimately fail for WA/NT.

    Takes the LAST match rather than the first. A street name can
    legitimately contain a state abbreviation followed by digits
    ("Victoria SA 5000" as a suburb inside a longer address), and the
    state/postcode pair is always the trailing component before the
    country.
    """
    if not address:
        return None, None
    matches = _STATE_POSTCODE_RE.findall(address)
    if not matches:
        return None, None
    state, postcode = matches[-1]
    return state.upper(), postcode


def nem_region_for_state(state: str | None) -> str | None:
    """The NEM region id for an Australian state abbreviation, or None
    for one that is not in the NEM (WA, NT) or not recognised."""
    if not state:
        return None
    return _STATE_TO_NEM_REGION.get(state.upper())


def postcode_prefix(postcode: str | None) -> str | None:
    """The first three digits of a 4-digit postcode -- the schema's own
    stated privacy boundary, so a record identifies a coarse area rather
    than a household.

    Returns None for anything that is not exactly four digits rather
    than truncating whatever it was given: a 3-digit prefix derived from
    a malformed value would look valid to the schema while being wrong,
    which is the failure mode worth refusing.
    """
    if not postcode or not re.fullmatch(r"\d{4}", postcode):
        return None
    return postcode[:3]


def resolve_region_and_prefix(address: str | None) -> tuple[str | None, str | None]:
    """`(region, postcode_prefix)` from a formatted address -- the one
    call a consumer normally wants.

    Either element can be None independently: a WA address yields a real
    postcode prefix with no NEM region, and an unparseable address
    yields neither. Both are returned as-is rather than failing the pair,
    because the two fields fail for genuinely different reasons and a
    caller may reasonably use one without the other.
    """
    state, postcode = parse_state_and_postcode(address)
    return nem_region_for_state(state), postcode_prefix(postcode)
