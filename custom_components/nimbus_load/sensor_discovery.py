"""Entity auto-discovery rules, as plain functions the tests can call.

nimbus issue #954 (Mark Purcell, 48-hour IV&V #950). The two discovery
rules below used to live as methods on `NimbusSolverConfigSensor`, and
both were covered only by hand-copied mirrors of their logic inside
`tests/test_nem_region.py` and `tests/test_aemo_30min_forecast_
discovery.py` -- grepping `tests/` for either method name returned
nothing. The mirrors agreed with the real code, but agreement is not
coverage: any future edit to the shipped methods would have kept the
whole suite green while the shipped path quietly lost its only tests.

That is the "tested helper wired to nothing" shape this project has
already been burned by twice (#538, #692), which is why the fix is
extraction rather than another mirror.

Everything here takes an iterable of state-like objects -- anything with
`.entity_id` and `.state`, which `hass.states.async_all("sensor")`
already yields -- so the rules are importable and callable without a
Home Assistant instance, matching how `solver/` and `ml/` are already
tested.

The shared shape of both rules, and the reason they are one module:
filter by suffix, drop states that cannot answer, then require EXACTLY
one survivor. Refusing to guess between several is this project's
settled posture for discovery (#768's power-sensor discovery first, then
#495 and #452), because picking whichever sorts first tags real output
with a silently-chosen source.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

# Same dual-mode import every other project-internal module here uses
# (see solver_writer.py's own household_modes/solver_publish imports).
# The relative form is what the integration loads; the flat form is what
# `tests/_solver_path.py` produces by putting this directory on sys.path,
# which is how #954's whole point -- tests calling the SHIPPED rule
# rather than a copy of it -- is reachable without a Home Assistant
# instance.
try:
    from . import nem_region
except ImportError:  # pragma: no cover - flat/standalone import path
    # Both codes, unlike the older dual-mode sites in solver_writer.py
    # that carry `no-redef` alone: mypy resolves the flat form as
    # import-not-found too, and those sites simply leave that error
    # standing in the advisory run. No reason to add a 24th.
    import nem_region  # type: ignore[no-redef,import-not-found]

# A state that cannot answer the question being asked. Filtered out
# BEFORE the "exactly one" count, not merely parsed and found wanting --
# the correction v0.94.300 had to make to geocoded discovery after a real
# install turned up its single geocoded sensor reading `unavailable`
# (phone off, or out of range). Parsing that string happens to yield
# (None, None), but only by accident: there is no state/postcode pair in
# the word "unavailable".
#
# Filtering first makes "exactly one" mean exactly one USABLE sensor,
# which is the question actually being asked, and fixes a real case --
# two phones, one unavailable, should resolve from the other rather than
# be refused as ambiguous when only one of them can answer.
# Exactly the three the two methods already filtered on, unchanged. This
# extraction is deliberately behaviour-identity: widening the set (an
# empty state string is the obvious candidate) would be a real change to
# live discovery smuggled in under a refactor, and belongs in its own
# issue if it is wanted at all.
UNUSABLE_STATES = frozenset({None, "unknown", "unavailable"})

GEOCODED_LOCATION_SUFFIX = "_geocoded_location"
AEMO_30MIN_FORECAST_SUFFIX = "_current_30min_forecast"


class StateLike(Protocol):
    """The two attributes these rules need from an HA state object."""

    entity_id: str
    state: str


def usable_candidates(states: Iterable[StateLike], suffix: str) -> list[StateLike]:
    """Every state whose entity_id ends with `suffix` and which is in a
    state that can actually answer."""
    return [
        st
        for st in states
        if st.entity_id.endswith(suffix) and st.state not in UNUSABLE_STATES
    ]


def discover_unique_entity_id(states: Iterable[StateLike], suffix: str) -> str | None:
    """The one usable entity_id ending in `suffix`, or None when there
    is not exactly one.

    None for zero and None for several, deliberately: a household
    running two of something genuinely has two answers, and this project
    refuses rather than picking one.
    """
    candidates = usable_candidates(states, suffix)
    if len(candidates) != 1:
        return None
    return candidates[0].entity_id


def discover_aemo_30min_forecast_sensor(states: Iterable[StateLike]) -> str | None:
    """The `sensor.aemo_nem_<region>_current_30min_forecast` entity, or
    None when there is not exactly one usable candidate (nimbus issue
    #452).

    **Why matching this suffix is not the region-name hardcoding this
    project already fixed once.** `const.py` records that hardcoding one
    specific region's entity (`sensor.aemo_nem_qld1_...`) was a real
    bug: a NSW household could never reach it. This matches the SUFFIX
    only, so every region resolves through the identical code path and
    nobody's region is privileged.
    """
    return discover_unique_entity_id(states, AEMO_30MIN_FORECAST_SUFFIX)


def resolve_geocoded_region_and_prefix(
    states: Iterable[StateLike],
) -> tuple[str | None, str | None]:
    """(region, postcode_prefix) from the Companion App's own
    `sensor.<device>_geocoded_location`, or (None, None).

    nimbus issue #495 (Signals 6/7 of #489). `region` and
    `postcode_prefix` are both in the telemetry schema's own top-level
    `required` array and neither existed anywhere in Nimbus. Mark
    Purcell's answer, after checking what was already installed rather
    than proposing a new mechanism: "Use companion app reverse geocode
    as a one time setup."

    `_geocoded_location` is Home Assistant's own documented Companion
    App naming, not a third-party's arbitrary choice -- which is why
    matching on it is not the same mistake as inferring a
    battery/solar/grid role from a sensor's name (the reason the Power
    Signal `signal_role` dropdown exists). It reads an already-installed
    integration's entity; Nimbus makes no outbound geocoding call of its
    own.

    More than one registered phone means more than one such sensor, and
    they can legitimately disagree (two people in two places). Refuses
    rather than picking one.

    Mark's own caveat, worth keeping visible: this tracks wherever the
    phone currently is, not the fixed installation address. Fine as the
    coarse region/prefix the schema asks for, and the reason his
    instruction said *one time* setup -- a consumer should read this
    once and store it, not re-derive it per record.
    """
    candidates = usable_candidates(states, GEOCODED_LOCATION_SUFFIX)
    if len(candidates) != 1:
        return None, None
    return nem_region.resolve_region_and_prefix(candidates[0].state)
