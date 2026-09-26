"""nimbus issue #467, staged item 3: resolve an HA calendar entity's upcoming
events into adequacy windows.

## Why this is the item that was actually missing

#467 (@purcell-lab) named four staged items and called this one *"the one
genuinely **new** capability, not a composition of existing primitives"*. That
assessment held up when checked three weeks later — the other three had either
already shipped or shipped since:

| item | state |
|---|---|
| 1. penalized shortfall slack on a deadline load | **done** — `AdequacyLoadConfig.shortfall_price` / `shortfall_kwh`, explicitly the #390 `grid_import_excess` pattern applied to a different constraint |
| 2. `build_plan()` over a list of storage participants | **done** — `build_plan(batteries: list[BatteryConfig], …)`, participant subentries, `available` / `unavailable_until_period_index` gating, away-period zeroing in scoring (#1098) |
| 3. calendar-driven window/target resolution | **this** — nothing anywhere in the codebase read a `calendar.*` entity |
| 4. a real end-to-end scenario | follows this |

## Zero HAEO, and one deliberate divergence from it

The architecture is read from HAEO's own EV stack (#366/#492/#494/#495/#496) as
prior art and reimplemented against HA's own calendar API — no HAEO import,
entity or code path, per the standing directive.

**The divergence is not cosmetic and is the most important thing in this file.**
HAEO's #495 models the trip as a *deferrable sink*: "capacity opens at trip
start, requirement due at trip end" — i.e. it schedules the **driving
consumption**. This module models the **charging obligation**: deliver
`target_kwh` *by departure*, so the deadline is the period containing the event
**start**.

Copying HAEO's shape here would be wrong, because Nimbus already zeroes a
participant's charge/discharge for every away period
(`_resolve_battery_participant_history()`, #1098/#768) — a car's propulsion draw
never touches this household's grid connection and is deliberately not priced
against it. So there is no during-trip consumption left for a sink to schedule;
what matters is that the energy is in the pack before the car leaves.

## The failure mode this file exists to prevent

#467 is explicit about it: HAEO's own first EV attempt (#361) was closed as
*"functionally inert with zero capacity"* because **the calendar-to-capacity
wiring was never actually exercised end-to-end** despite the schema looking
complete. Mark's item 4 asks for a real scenario test before any of this is
called done, for exactly that reason.

So this file tests the resolution layer hard and in isolation, and it does **not**
claim the feature is wired. `TestTheBoundaryIsDeliberate` pins why the layer is
separable at all.

## The judgement calls, each pinned below

* **A distance needs a unit.** `"Sydney 400"` is far more likely a flight number,
  a room or a price than kilometres. An event with no parseable distance produces
  **no window and a warning** — the alternative, a default trip size, silently
  commits real battery capacity to an invented number.
* **`None` and `0.0` are different answers.** `"0 km"` is a legitimate parse (a
  cancelled trip); "no distance stated" is not, and they must not collapse.
* **Consecutive trips cannot share one charge.** The second window's
  `earliest_period` is the first's deadline, or two Tuesday trips could both be
  satisfied by a single Monday charge.
* **A departure inside the current period is dropped**, not reported as a
  shortfall. No period remains in which to charge for it, so emitting the window
  would turn a finished decision into a permanent complaint.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "calendar_trips.py"
)


def _load_module():
    """Load the module by path, with no Home Assistant import anywhere.

    Deliberately not `from custom_components.nimbus_load.solver_inputs import
    calendar_trips`: that executes the package `__init__`, which imports HA's
    config-entry machinery. This module has zero HA dependencies and this loader
    is what proves it -- if an HA import is ever added, this file fails first.
    """
    spec = importlib.util.spec_from_file_location("calendar_trips", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `dataclasses` resolves annotations through
    # `sys.modules[cls.__module__]` and raises AttributeError without it.
    sys.modules["calendar_trips"] = module
    spec.loader.exec_module(module)
    return module


ct = _load_module()
TripEvent = ct.TripEvent

# Brisbane, tz-AWARE on purpose: HA hands calendar events aware datetimes,
# and a naive fixture would test a shape production never sees.
_AEST = timezone(timedelta(hours=10))
_T0 = datetime(2026, 9, 27, 0, 0, tzinfo=_AEST)
_GRID = [_T0 + timedelta(minutes=30 * i) for i in range(48)]  # 24h @ 30 min
_END = _GRID[-1] + timedelta(minutes=30)


def _at(hours: float) -> datetime:
    return _T0 + timedelta(hours=hours)


def _windows(events, **kw):
    kw.setdefault("kwh_per_100km", 18.0)
    kw.setdefault("horizon_end", _END)
    return ct.resolve_trip_windows(events, _GRID, **kw)


class TestADistanceNeedsAUnit(unittest.TestCase):
    """A bare number must not be read as kilometres."""

    def test_real_distances_parse(self):
        for text, expected in [
            ("Trip to Brisbane 120 km", 120.0),
            ("school run 18km", 18.0),
            ("commute 8 kilometres", 8.0),
            ("Trip 5 KM", 5.0),
            ("drive 8 kilometers", 8.0),
        ]:
            with self.subTest(text=text):
                self.assertAlmostEqual(ct.parse_trip_distance_km(text), expected)

    def test_miles_convert(self):
        self.assertAlmostEqual(ct.parse_trip_distance_km("30 mi"), 48.28032)
        self.assertAlmostEqual(ct.parse_trip_distance_km("12.5 miles"), 20.1168)
        self.assertAlmostEqual(ct.parse_trip_distance_km("1 mile"), 1.609344)

    def test_a_bare_number_is_not_a_distance(self):
        """ "Sydney 400" is a flight number far more often than 400 km."""
        self.assertIsNone(ct.parse_trip_distance_km("Sydney 400"))
        self.assertIsNone(ct.parse_trip_distance_km("board meeting"))
        self.assertIsNone(ct.parse_trip_distance_km(""))

    def test_minutes_are_not_miles(self):
        """The regex ends on a word boundary specifically so `mi` cannot match
        the start of `min`. Without it, "50 min" becomes an 80 km trip."""
        self.assertIsNone(ct.parse_trip_distance_km("50 min"))
        self.assertIsNone(ct.parse_trip_distance_km("30 minutes"))

    def test_zero_km_is_a_real_answer_not_a_failure(self):
        """A cancelled trip states 0 km. Collapsing that into None would make it
        indistinguishable from an event nobody put a distance on, and the two
        want opposite handling -- one is known-zero, the other is unknown."""
        self.assertEqual(ct.parse_trip_distance_km("0 km"), 0.0)
        self.assertIsNotNone(ct.parse_trip_distance_km("0 km"))

    def test_the_summary_wins_over_the_description(self):
        """Both stating a distance is a conflict, and the summary is the more
        deliberate of the two."""
        self.assertEqual(ct.parse_trip_distance_km("trip 50 km", "notes: 999 km"), 50.0)

    def test_the_description_is_used_when_the_summary_is_silent(self):
        self.assertEqual(
            ct.parse_trip_distance_km("Brisbane", "220 km each way"), 220.0
        )


class TestTheEnergyIsNeverNegative(unittest.TestCase):
    def test_a_plain_trip(self):
        self.assertAlmostEqual(ct.trip_energy_kwh(100.0, 18.0), 18.0)

    def test_the_odometer_reduces_what_is_still_owed(self):
        self.assertAlmostEqual(
            ct.trip_energy_kwh(100.0, 18.0, already_driven_km=60.0), 7.2
        )

    def test_over_driving_clamps_to_zero_rather_than_crediting(self):
        """A negative target_kwh would be accepted downstream and quietly invert
        the constraint's meaning -- the LP would be told to END the window with
        less energy than it started."""
        self.assertEqual(ct.trip_energy_kwh(100.0, 18.0, already_driven_km=150.0), 0.0)


class TestTheIncrementToLevelConversion(unittest.TestCase):
    """`trip_kwh` is an INCREMENT; `must_have_soc_kwh` is a LEVEL.

    Checked rather than assumed, and it changed which primitive this feeds. An EV
    departure belongs on the participant battery's own
    `must_have_soc_by_period_index` / `must_have_soc_kwh` -- routing it through an
    `AdequacyLoadConfig` would be double counting, because an adequacy load is a
    separate sink that must absorb the energy the EV pack already absorbs as a
    storage participant. The LP would be told to buy it twice.
    """

    def test_the_trip_sits_ABOVE_the_reserve_floor(self):
        """Passing the increment straight through under-requires by exactly
        `min_soc_kwh` -- the trip would be planned to start from an empty pack
        and drive into the reserve the household said it would not touch."""
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(5.0, 12.0), 17.0)

    def test_a_trip_bigger_than_the_pack_asks_to_arrive_FULL(self):
        """A 400 km drive in a 50 kWh car is a real configuration. Demanding more
        than the ceiling would make the constraint infeasible -- and this
        primitive has no slack, so it would take the whole plan down, the exact
        failure #390 fixed for grid import and #467 asked to avoid."""
        self.assertAlmostEqual(
            ct.trip_must_have_soc_kwh(5.0, 100.0, max_soc_kwh=40.0), 40.0
        )

    def test_a_negative_trip_cannot_lower_the_floor(self):
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(5.0, -3.0), 5.0)

    def test_without_a_ceiling_it_does_not_invent_one(self):
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(2.0, 60.0), 62.0)


class TestTheWindowEndsAtDEPARTURE(unittest.TestCase):
    """The deliberate divergence from HAEO's model -- see the file docstring."""

    def test_the_deadline_is_the_period_containing_the_event_start(self):
        out = _windows([TripEvent(_at(7), _at(9), "school run 18km")])
        self.assertEqual(len(out), 1)
        earliest, deadline, target = out[0]
        self.assertEqual(
            _GRID[deadline],
            _at(7),
            "the charge is due before the car leaves, not when it returns",
        )
        self.assertEqual(earliest, 0, "the first trip can charge from now")
        self.assertAlmostEqual(target, 3.24)  # 18 km * 18 kWh/100km

    def test_the_deadline_is_not_the_event_end(self):
        """Pinned as its own test because copying HAEO's "requirement due at
        trip end" would price driving energy as household import."""
        out = _windows([TripEvent(_at(7), _at(9), "school run 18km")])
        self.assertNotEqual(_GRID[out[0][1]], _at(9))


class TestConsecutiveTripsCannotShareOneCharge(unittest.TestCase):
    def test_the_second_window_starts_where_the_first_ended(self):
        """Without this, a single charge placed before both trips satisfies
        both -- the pack would arrive at the second trip already drained by the
        first."""
        out = _windows(
            [
                TripEvent(_at(7), _at(9), "school run 18km"),
                TripEvent(_at(14), _at(18), "Trip to Brisbane 120 km"),
            ]
        )
        self.assertEqual(len(out), 2)
        first_earliest, first_deadline, _ = out[0]
        second_earliest, second_deadline, second_target = out[1]
        self.assertEqual(first_earliest, 0)
        self.assertEqual(second_earliest, first_deadline)
        self.assertGreater(second_deadline, first_deadline)
        self.assertAlmostEqual(second_target, 21.6)  # 120 km * 18/100

    def test_events_are_ordered_by_start_regardless_of_input_order(self):
        """A calendar service is not required to return events sorted, and
        chaining `earliest` off the previous deadline is only correct in
        chronological order."""
        out = _windows(
            [
                TripEvent(_at(14), _at(18), "Trip to Brisbane 120 km"),
                TripEvent(_at(7), _at(9), "school run 18km"),
            ]
        )
        self.assertEqual([round(w[2], 2) for w in out], [3.24, 21.6])


class TestEventsThatMustNotProduceAWindow(unittest.TestCase):
    def test_no_distance_is_skipped_and_warned_about_loudly(self):
        """Silence here is how HAEO's #361 died -- a schema that looked
        complete over wiring that did nothing."""
        with self.assertLogs(ct._LOGGER, level="WARNING") as cm:
            out = _windows([TripEvent(_at(9), _at(10), "dinner")])
        self.assertEqual(out, ())
        joined = "\n".join(cm.output)
        self.assertIn("#467", joined)
        self.assertIn("no distance", joined)
        self.assertIn("IGNORED", joined, "say what was done, not just what was seen")

    def test_a_departure_in_the_current_period_is_dropped(self):
        """No period remains in which to charge for it. Emitting the window
        would convert a decision that is already made into a permanent
        reported shortfall."""
        self.assertEqual(_windows([TripEvent(_T0, _at(2), "leaving now 50 km")]), ())

    def test_an_event_wholly_before_the_horizon_is_dropped(self):
        past = TripEvent(_T0 - timedelta(hours=5), _T0 - timedelta(hours=4), "40 km")
        self.assertEqual(_windows([past]), ())

    def test_an_event_beyond_the_horizon_is_dropped(self):
        later = TripEvent(_END + timedelta(hours=2), _END + timedelta(hours=3), "40 km")
        self.assertEqual(_windows([later]), ())

    def test_a_fully_driven_trip_needs_no_charge(self):
        out = _windows(
            [TripEvent(_at(7), _at(9), "trip 100 km")],
            already_driven_km_by_start={_at(7): 100.0},
        )
        self.assertEqual(out, ())

    def test_an_empty_grid_returns_nothing_rather_than_raising(self):
        """A solve with no periods is a degenerate config, not a crash site."""
        self.assertEqual(
            ct.resolve_trip_windows(
                [TripEvent(_at(7), _at(9), "40 km")],
                [],
                kwh_per_100km=18.0,
                horizon_end=_END,
            ),
            (),
        )

    def test_a_usable_trip_still_lands_when_another_is_skipped(self):
        """One unparseable event must not suppress the rest -- the same
        defensive posture the sweep paths in this codebase already take."""
        out = _windows(
            [
                TripEvent(_at(7), _at(9), "dinner"),
                TripEvent(_at(14), _at(18), "Trip to Brisbane 120 km"),
            ]
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][2], 21.6)


class TestTheBoundaryIsDeliberate(unittest.TestCase):
    """Why this layer is separable, and must stay so."""

    def test_it_imports_nothing_from_home_assistant(self):
        """The whole reason this file can test the logic exhaustively rather
        than through a service mock. Also why it survives the local HA version
        being older than the one production runs."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("import homeassistant", "from homeassistant"):
            self.assertNotIn(forbidden, source)

    def test_it_imports_nothing_from_the_solver_elements(self):
        """Windows are returned as plain tuples, so the dependency points one
        way: this module resolves calendar data into numbers and the caller
        decides which element to build."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("from ..solver", source)
        self.assertNotIn("import AdequacyWindow", source)

    def test_it_mentions_no_haeo_entity_or_import(self):
        """The standing ZERO HAEO directive. HAEO is referenced in prose as
        prior art, which is intended; an import or an entity id would not be."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import haeo", source)
        self.assertNotIn("sensor.haeo", source)
        self.assertNotIn("number.haeo", source)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
