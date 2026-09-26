"""Resolve an HA calendar entity's upcoming events into trip charge requirements
(nimbus issue #467, staged item 3).

Each usable event becomes `(earliest_period, deadline_period, target_kwh)`. What
element that maps onto is the caller's decision, and getting it wrong is a real
risk -- see "Which primitive consumes this" below.

**Why this module exists at all.** #467's own staged scope named four items and
called this one "the one genuinely *new* capability, not a composition of
existing primitives". The other three were either already present or built since:
`AdequacyLoadConfig.shortfall_price`/`shortfall_kwh` gives a deadline a priced
slack instead of a hard constraint (the #390 `grid_import_excess` pattern applied
to a different constraint), `build_plan()` already takes a list of storage
participants, and participant availability is gated by `available` /
`unavailable_until_period_index`. Nothing anywhere in this codebase reads a
`calendar.*` entity.

**Zero HAEO, per the standing directive.** The architecture lesson is borrowed
from HAEO's own 5-PR EV stack (#366/#492/#494/#495/#496) -- read as prior art and
reimplemented here against HA's own calendar API. No HAEO import, sensor, entity
or code path is involved.

## The one place this deliberately departs from HAEO's model

HAEO's #495 composes an EV so that the trip requirement is a *deferrable sink*:
"capacity opens at trip start, requirement due at trip end". That models the
**driving consumption** -- energy leaving the pack while away.

This module models the **charging obligation** instead: deliver `target_kwh` into
the pack **by departure**, so `deadline_period` is the period containing the event
**start**, not its end. Same calendar event, different question, and the
difference is not cosmetic:

* Nimbus already zeroes a participant's charge/discharge for every period it is
  away (`_resolve_battery_participant_history()`, #1098/#768) -- the away
  consumption is deliberately *not* priced against this household's grid, because
  a car's propulsion draw never touches it.
* So there is nothing here for a during-trip sink to schedule. What matters is
  that the energy is present before the car leaves, which is a requirement
  falling due at departure.

Modelling it HAEO's way would price driving energy as household import. Stating
the divergence rather than silently copying the shape.

## What a real calendar event has to contain

A distance, in the summary or description: `"Trip to Brisbane 120 km"`,
`"school run 18km"`, `"30 mi"`, `"12.5 miles"`. Parsed case-insensitively,
decimals allowed, miles converted at 1.609344.

**An event with no parseable distance produces no window, and says so.** The
alternative -- assuming a default trip size -- would silently commit a household's
battery to an invented number. HAEO's own abandoned first attempt (#361) failed
in the opposite direction: the schema implied trip capacity was computed from the
calendar while the wiring was never connected, leaving the trip battery
"functionally inert with zero capacity". Both failure modes are silent; this one
at least logs.

## Which primitive consumes this, and the one that would be WRONG

Checked rather than assumed, and it changed the answer. An EV departure belongs
on the participant battery's own SoC floor --
`BatteryConfig.must_have_soc_by_period_index` / `must_have_soc_kwh` -- which
already exists, validates as a pair (#563 item 2), reaches the LP, and reaches
the scorer's oracle too (#1111 fixed exactly that drift).

Routing it through an `AdequacyLoadConfig` instead would be **double counting**:
an adequacy load is a separate sink that must absorb `target_kwh`, and the EV
pack already absorbs that charge as a storage participant. The LP would be told
to buy the energy twice.

So this module resolves a trip into *timing and a quantity*, and the caller maps
it onto the right element:

* an **EV/participant battery** -> `must_have_soc_*` (see
  `trip_must_have_soc_kwh()` below for the increment-to-level conversion);
* a genuinely separate deadline load with no storage of its own (a hot-water
  element, say) -> an adequacy window, where `earliest_period` matters.

`earliest_period` is returned either way because it is meaningful for the second
case and free for the first.

## Odometer correction

If a trip is already under way, part of its distance has been driven and the
remaining obligation is smaller. `already_driven_km` subtracts it. Optional,
because most installs have no odometer sensor, and absent it the requirement is
simply the full trip -- conservative in the right direction (the pack ends up
fuller than strictly needed, never emptier).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Deliberately requires a unit. A bare number in an event title ("Sydney 400")
# is far more likely to be a flight number, a room, or a price than kilometres,
# and guessing would commit real battery capacity to a coincidence.
#
# Ordered longest-unit-first so "miles" cannot be matched as "mi" with a
# trailing "les", and \b-anchored at the end so "50 min" never reads as 50 mi.
_DISTANCE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>kilometres|kilometers|kilometre|kilometer|miles|mile|km|mi)\b",
    re.IGNORECASE,
)

_MILES_TO_KM = 1.609344
_MILE_UNITS = frozenset({"miles", "mile", "mi"})


@dataclass(frozen=True)
class TripEvent:
    """One calendar event, reduced to only what sizing a charge needs.

    Deliberately not HA's own event dict: this module is unit-testable without
    Home Assistant, and the caller that does talk to HA
    (`calendar.get_events` via `ha_call_service_with_response`) is responsible
    for the mapping. That boundary is what lets the parsing and window logic be
    tested exhaustively rather than through a service mock.
    """

    start: datetime
    end: datetime
    summary: str = ""
    description: str = ""


def parse_trip_distance_km(*texts: str) -> float | None:
    """Distance in km from any of `texts`, or None if none states one.

    First match wins, scanning the arguments in order -- so a caller passing
    `(summary, description)` gets the summary's own figure when both state one,
    which is the more deliberate of the two.

    Returns None rather than 0.0 for "no distance found", because 0.0 is a
    legitimate parse (`"0 km"`, a cancelled trip) and must not be
    indistinguishable from an unparseable one.
    """
    for text in texts:
        if not text:
            continue
        match = _DISTANCE_RE.search(text)
        if match is None:
            continue
        value = float(match.group("value"))
        if match.group("unit").lower() in _MILE_UNITS:
            value *= _MILES_TO_KM
        return value
    return None


def trip_energy_kwh(
    distance_km: float,
    kwh_per_100km: float,
    *,
    already_driven_km: float = 0.0,
) -> float:
    """The charge a trip still requires, in kWh.

    Never negative: an odometer reading beyond the planned distance means the
    trip is already over-driven, and the remaining obligation is zero, not a
    credit. A negative `target_kwh` would be accepted by `AdequacyWindow` and
    then quietly invert the constraint's meaning.
    """
    remaining_km = max(0.0, float(distance_km) - float(already_driven_km))
    return remaining_km * float(kwh_per_100km) / 100.0


def trip_must_have_soc_kwh(
    min_soc_kwh: float,
    trip_kwh: float,
    *,
    max_soc_kwh: float | None = None,
) -> float:
    """A trip's kWh requirement as an ABSOLUTE SoC level for
    `BatteryConfig.must_have_soc_kwh`.

    The conversion is the whole point of this function existing rather than the
    caller doing it inline: `trip_kwh` is an INCREMENT (energy the trip will
    consume) while `must_have_soc_kwh` is a LEVEL (where the pack must be at the
    deadline). Passing the increment straight through would under-require by
    exactly `min_soc_kwh` -- the trip would be planned to start from an empty
    pack and drive into the reserve floor.

    So the requirement is `min_soc_kwh + trip_kwh`: the trip's energy must sit
    ABOVE the reserve the household has said it will not go below.

    Clamped to `max_soc_kwh` when given, because a trip larger than the usable
    pack is a real configuration (a 400 km drive in a 50 kWh car) and the honest
    LP request is "arrive full". Demanding more than the ceiling would make the
    constraint infeasible and, with no slack on this primitive, take the whole
    plan down -- the failure mode #390 fixed for grid import and #467 asked to
    avoid here. The shortfall is then real and physical, and belongs in a
    warning at the call site rather than in an impossible constraint.
    """
    required = float(min_soc_kwh) + max(0.0, float(trip_kwh))
    if max_soc_kwh is not None:
        return min(float(max_soc_kwh), required)
    return required


def resolve_trip_deadline(
    events: list[TripEvent],
    grid_times: list[datetime],
    *,
    kwh_per_100km: float,
    horizon_end: datetime,
    min_soc_kwh: float,
    max_soc_kwh: float,
    already_driven_km_by_start: dict[datetime, float] | None = None,
) -> tuple[int, float] | None:
    """The soonest trip as a `(must_have_soc_by_period_index, must_have_soc_kwh)`
    pair, or None when no trip in this horizon needs charge (nimbus issue #467,
    staged item 4).

    This is the whole calendar-to-LP chain in one call, and it exists so the
    chain is exercised end to end rather than assembled differently at each call
    site. #467 asks for exactly that: HAEO's own first EV attempt (#361) was
    closed as "functionally inert with zero capacity" because the
    calendar-to-capacity wiring was never actually run, despite the schema
    looking complete.

    **Only the SOONEST trip is returned, and that is a real limitation rather
    than a simplification.** `BatteryConfig` carries ONE
    `must_have_soc_by_period_index`/`must_have_soc_kwh` pair, so one deadline is
    all the element can express. Returning the first is the right choice of the
    available ones: it is the binding constraint in time, and a later trip in the
    same horizon gets its own chance on a later solve, by which point it will be
    the soonest. Silently dropping the others would be wrong to leave
    undocumented, so `resolve_trip_windows()` still returns them all and the
    caller can see what was set aside.

    Returns None -- explicitly, rather than a zero requirement -- when there is
    no usable trip. A zero `must_have_soc_kwh` is NOT the same thing: paired with
    an index it would pin the pack to its own floor at that period, which is a
    real constraint nobody asked for.
    """
    windows = resolve_trip_windows(
        events,
        grid_times,
        kwh_per_100km=kwh_per_100km,
        horizon_end=horizon_end,
        already_driven_km_by_start=already_driven_km_by_start,
    )
    if not windows:
        return None
    _earliest, deadline, trip_kwh = windows[0]
    if len(windows) > 1:
        _LOGGER.info(
            "Nimbus calendar (#467): %d trips fall in this horizon; planning for "
            "the soonest (period %d, %.2f kWh). A BatteryConfig carries one "
            "departure deadline, so the later %d will be planned for on the "
            "solves where they are the soonest.",
            len(windows),
            deadline,
            trip_kwh,
            len(windows) - 1,
        )
    return deadline, trip_must_have_soc_kwh(
        min_soc_kwh, trip_kwh, max_soc_kwh=max_soc_kwh
    )


def _period_index_at(grid_times: list[datetime], when: datetime) -> int | None:
    """The index of the period containing `when`, or None if outside the grid.

    The grid is the solve's own period start times, so the containing period is
    the last one starting at or before `when`. A time before the first period
    belongs to no period the solver can act in.
    """
    if not grid_times or when < grid_times[0]:
        return None
    index = 0
    for i, start in enumerate(grid_times):
        if start <= when:
            index = i
        else:
            break
    # `when` beyond the final period's start is only inside the horizon if it
    # falls within that last period -- which the caller bounds by comparing
    # against the horizon end, since this function has no period width.
    return index


def resolve_trip_windows(
    events: list[TripEvent],
    grid_times: list[datetime],
    *,
    kwh_per_100km: float,
    horizon_end: datetime,
    already_driven_km_by_start: dict[datetime, float] | None = None,
) -> tuple[tuple[int, int, float], ...]:
    """`(earliest_period, deadline_period, target_kwh)` per usable trip.

    Returned as plain tuples rather than `AdequacyWindow` instances so this
    module imports nothing from `solver.elements` -- it resolves calendar data
    into numbers, and the caller decides what element to build. That keeps the
    dependency pointing one way and this module testable on its own.

    **The window shape**, and why each end is where it is:

    * `deadline_period` is the period containing the event's **start**. See the
      module docstring: the obligation is to have the energy in the pack before
      departure, not to schedule the driving.
    * `earliest_period` is 0 for the first trip, and thereafter the deadline of
      the previous trip -- charging for a Tuesday trip must not be credited
      against a Monday one it happened before. Without this, two trips in one
      horizon can be satisfied by a single charge placed before both.

    Events are skipped, each with its own reason logged, when they:

    * state no parseable distance -- never guess a trip size;
    * end before the horizon starts, or start after it ends -- nothing to plan;
    * resolve to a deadline at period 0 -- the departure is already inside the
      current period, so no period remains in which to charge for it. Emitting
      it anyway would hand the LP a window it cannot satisfy and turn a
      finished decision into a reported shortfall.
    """
    if not grid_times:
        return ()
    already_driven = already_driven_km_by_start or {}
    windows: list[tuple[int, int, float]] = []
    previous_deadline = 0
    for event in sorted(events, key=lambda e: e.start):
        if event.end < grid_times[0] or event.start > horizon_end:
            _LOGGER.debug(
                "Nimbus calendar (#467): skipping trip %r -- outside the solve "
                "horizon (%s..%s)",
                event.summary,
                grid_times[0].isoformat(),
                horizon_end.isoformat(),
            )
            continue
        distance_km = parse_trip_distance_km(event.summary, event.description)
        if distance_km is None:
            _LOGGER.warning(
                "Nimbus calendar (#467): calendar event %r has no distance in "
                "its summary or description, so the charge it needs cannot be "
                "sized and it is being IGNORED. Add a distance with a unit "
                "(e.g. '120 km' or '75 mi') to have Nimbus charge for it.",
                event.summary,
            )
            continue
        deadline = _period_index_at(grid_times, event.start)
        if deadline is None or deadline == 0:
            _LOGGER.debug(
                "Nimbus calendar (#467): skipping trip %r -- its departure is "
                "at or before the current period, so no period remains to "
                "charge for it",
                event.summary,
            )
            continue
        target_kwh = trip_energy_kwh(
            distance_km,
            kwh_per_100km,
            already_driven_km=float(already_driven.get(event.start, 0.0)),
        )
        if target_kwh <= 0.0:
            _LOGGER.debug(
                "Nimbus calendar (#467): skipping trip %r -- it requires no "
                "further charge (distance %.1f km already driven)",
                event.summary,
                distance_km,
            )
            continue
        earliest = min(previous_deadline, deadline)
        windows.append((earliest, deadline, target_kwh))
        previous_deadline = deadline
    return tuple(windows)
