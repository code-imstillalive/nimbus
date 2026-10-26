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


class TestTheIncrementToLevelConversion(unittest.TestCase):
    """`trip_kwh` is an INCREMENT; `must_have_soc_kwh` is a LEVEL.

    Checked rather than assumed, and it changed which primitive this feeds. An EV
    departure belongs on the participant battery's own
    `must_have_soc_by_period_index` / `must_have_soc_kwh` -- routing it through an
    `AdequacyLoadConfig` would be double counting, because an adequacy load is a
    separate sink that must absorb the energy the EV pack already absorbs as a
    storage participant. The LP would be told to buy it twice.
    """

    def test_a_negative_trip_cannot_lower_the_floor(self):
        self.assertAlmostEqual(ct.trip_must_have_soc_kwh(5.0, -3.0), 5.0)


class TestEventsThatMustNotProduceAWindow(unittest.TestCase):
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
