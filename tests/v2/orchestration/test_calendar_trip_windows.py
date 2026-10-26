"""An HA calendar's upcoming trips become a participant's charging
windows: a trip's distance needs a unit, its energy is never negative,
it is asked for above the reserve floor, and it is due at DEPARTURE (the
period holding the event's start), with consecutive trips never sharing one
charge. An event with no distance, already under way, or outside the
horizon makes no window (#467) (#1837).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
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

    def test_miles_convert(self):
        self.assertAlmostEqual(ct.parse_trip_distance_km("30 mi"), 48.28032)
        self.assertAlmostEqual(ct.parse_trip_distance_km("12.5 miles"), 20.1168)
        self.assertAlmostEqual(ct.parse_trip_distance_km("1 mile"), 1.609344)

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

    def test_a_negative_trip_cannot_lower_the_floor(self):
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(5.0, -3.0), 5.0)

    def test_without_a_ceiling_it_does_not_invent_one(self):
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(2.0, 60.0), 62.0)


class TestTheWindowEndsAtDEPARTURE(unittest.TestCase):
    """The trip is a charging obligation, due before the car leaves: its
    driving energy never touches this home's grid connection."""

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
        """Silence here would be a schema that looks complete over wiring
        that does nothing."""
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
