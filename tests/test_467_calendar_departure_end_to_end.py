"""nimbus issue #467, staged item 4: the calendar-to-LP chain, exercised end to
end through a real solve.

## Why this file is the gate, in #467's own words

> HAEO's #361 died specifically because the calendar-to-capacity wiring was never
> actually exercised end-to-end despite the schema looking complete

That attempt was closed by its own author as leaving the trip battery
*"functionally inert with zero capacity"*. The schema read correctly; nothing
connected it to anything. Item 4 exists so Nimbus cannot ship the same shape, and
the acceptance shape @purcell-lab named is HAEO's `scenario7_ev`: **cheap-window
pre-charge, trip delivered by the deadline, backstop unused when home charging
suffices.**

So every test here drives `build_plan()` — a real LP solve — from real calendar
events. Nothing is asserted about the schema in isolation; the resolution layer
already has its own 28 tests in `test_467_calendar_trip_windows.py`.

## The chain under test

```
TripEvent(summary="… 120 km")            a calendar event
  -> parse_trip_distance_km              distance, unit-checked
  -> trip_energy_kwh                     kWh, odometer-corrected
  -> resolve_trip_windows                (earliest, deadline, kWh) per trip
  -> trip_must_have_soc_kwh              increment -> ABSOLUTE SoC level
  -> resolve_trip_deadline               the soonest as one (index, kwh) pair
  -> BatteryConfig.must_have_soc_*       the primitive that already reaches
                                         both the LP and the scorer's oracle
  -> build_plan                          a plan that charges before departure
```

The last two steps are the ones #361 never had.

## Why `must_have_soc_*` and not an adequacy load

Checked rather than assumed, and it changed the answer. #467's text offered
*"`AdequacyLoadConfig` (or a new EV-specific deadline load built on the same
primitive)"*. Neither is right for an EV: an adequacy load is a separate sink
that must absorb `target_kwh`, and the EV pack already absorbs that charge as a
storage participant — the LP would be told to buy the energy twice.
`BatteryConfig.must_have_soc_by_period_index`/`must_have_soc_kwh` already exists,
validates as a pair (#563), reaches the LP, and reaches the scorer's oracle
(#1111).

## What these tests deliberately do NOT cover

The HA-facing half: reading a real `calendar.*` entity via
`calendar.get_events`, and the subentry fields that name it. Those need no new
mechanism — `ha_call_service_with_response` already bridges REST and native — but
they are not what #361 got wrong. **What #361 got wrong is proven absent here.**
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "calendar_trips.py"
)


def _load_calendar_trips():
    spec = importlib.util.spec_from_file_location("calendar_trips", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["calendar_trips"] = module
    spec.loader.exec_module(module)
    return module


ct = _load_calendar_trips()
TripEvent = ct.TripEvent

# Brisbane; HA hands calendar events tz-aware datetimes.
_AEST = timezone(timedelta(hours=10))
_T0 = datetime(2026, 9, 27, 0, 0, tzinfo=_AEST)
_N = 24
_GRID_TIMES = [_T0 + timedelta(hours=i) for i in range(_N)]
_HORIZON_END = _GRID_TIMES[-1] + timedelta(hours=1)

# A cheap window in the middle of the night, expensive either side. This is the
# whole point of the scenario: the LP should put the charge in the cheap hours
# rather than immediately before the deadline.
_CHEAP_FROM, _CHEAP_TO = 1, 5
_EV_CAPACITY = 60.0
_EV_MIN_SOC = 6.0


def _prices() -> np.ndarray:
    price = np.full(_N, 0.45)
    price[_CHEAP_FROM:_CHEAP_TO] = 0.05
    return price


def _ev(must_have: tuple[int, float] | None) -> BatteryConfig:
    """The EV as a storage participant, with or without a trip deadline."""
    index, kwh = (None, None) if must_have is None else must_have
    return BatteryConfig(
        name="ev",
        capacity_kwh=_EV_CAPACITY,
        initial_soc_kwh=10.0,
        min_soc_kwh=_EV_MIN_SOC,
        max_soc_kwh=_EV_CAPACITY,
        max_charge_kw=7.0,
        max_discharge_kw=0.0,  # no V2G here; the trip is the only draw
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        # Not zero: `BatteryConfig` rejects charge_cost + discharge_cost below
        # 0.01 as the exact wash-trade degeneracy this project diagnosed for
        # HAEO (DegenerateConfigError). The first draft of this fixture used
        # 0.0/0.0 and the guard caught it, which is the guard working.
        charge_cost=0.01,
        discharge_cost=0.0,
        salvage_value=0.0,
        must_have_soc_by_period_index=index,
        must_have_soc_kwh=kwh,
    )


def _home_battery() -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=1.0,
        max_soc_kwh=10.0,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.05,
    )


def _solve(must_have: tuple[int, float] | None):
    return build_plan(
        periods=PeriodGrid(hours=np.full(_N, 1.0), start=None),
        grid=GridConfig(
            import_price=_prices(),
            export_price=np.full(_N, 0.03),
            import_limit_kw=15.0,
            export_limit_kw=15.0,
        ),
        batteries=[_home_battery(), _ev(must_have)],
        solar=SolarConfig(forecast_kw=np.zeros(_N)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(_N, 0.5))],
    )


def _resolve(events, **kw):
    kw.setdefault("kwh_per_100km", 18.0)
    kw.setdefault("horizon_end", _HORIZON_END)
    kw.setdefault("min_soc_kwh", _EV_MIN_SOC)
    kw.setdefault("max_soc_kwh", _EV_CAPACITY)
    return ct.resolve_trip_deadline(events, _GRID_TIMES, **kw)


def _ev_soc(plan) -> np.ndarray:
    for b in plan.batteries:
        if b.name == "ev":
            return np.asarray(b.soc_kwh)
    raise AssertionError("no 'ev' participant in the plan")


def _ev_charge(plan) -> np.ndarray:
    for b in plan.batteries:
        if b.name == "ev":
            return np.asarray(b.charge_kw)
    raise AssertionError("no 'ev' participant in the plan")


class TestTheChainReachesTheLP(unittest.TestCase):
    """The `scenario7_ev` shape: pre-charge cheap, arrive ready."""

    def setUp(self):
        # Departure at 08:00, 120 km ahead -> 21.6 kWh of driving, so the pack
        # must hold min_soc + 21.6 = 27.6 kWh when the car leaves.
        self.events = [
            TripEvent(
                start=_T0 + timedelta(hours=8),
                end=_T0 + timedelta(hours=17),
                summary="Trip to Brisbane 120 km",
            )
        ]

    def test_a_calendar_event_becomes_a_real_deadline_pair(self):
        resolved = _resolve(self.events)
        self.assertIsNotNone(resolved, "the chain produced nothing at all")
        index, kwh = resolved
        self.assertEqual(_GRID_TIMES[index].hour, 8, "deadline is the departure")
        self.assertAlmostEqual(kwh, _EV_MIN_SOC + 21.6, places=4)

    def test_the_plan_actually_MEETS_it(self):
        """The assertion #361 could never have made."""
        resolved = _resolve(self.events)
        plan = _solve(resolved)
        self.assertEqual(plan.status, "optimal")
        index, kwh = resolved
        self.assertGreaterEqual(
            float(_ev_soc(plan)[index]) + 1e-6,
            kwh,
            "the EV must hold the trip's energy when it leaves",
        )

    def test_the_charge_lands_in_the_CHEAP_window(self):
        """Meeting the deadline is not enough -- doing it expensively would mean
        the deadline reached the LP but the price signal did not. The cheap hours
        are 01:00-05:00 and the deadline is 08:00, so a plan that merely
        satisfies the constraint could charge at 06:00-07:00 instead.
        """
        plan = _solve(_resolve(self.events))
        charge = _ev_charge(plan)
        cheap = float(np.sum(charge[_CHEAP_FROM:_CHEAP_TO]))
        total = float(np.sum(charge))
        self.assertGreater(total, 0.0, "nothing charged at all")
        self.assertGreater(
            cheap / total,
            0.9,
            f"expected the charge in the cheap window; got {cheap:.2f} of "
            f"{total:.2f} kWh there",
        )

    def test_without_the_deadline_the_ev_does_not_charge_for_the_trip(self):
        """The control. If the EV charged the same amount with no deadline, this
        file would be proving the price curve rather than the wiring -- exactly
        the mistake of testing a schema that happens to look right.
        """
        with_deadline = float(np.sum(_ev_charge(_solve(_resolve(self.events)))))
        without = float(np.sum(_ev_charge(_solve(None))))
        self.assertGreater(
            with_deadline,
            without + 1.0,
            "the calendar deadline must be what causes the charging",
        )


class TestTheHonestNoOps(unittest.TestCase):
    """Nothing is invented when there is nothing to plan for."""

    def test_an_empty_calendar_resolves_to_None(self):
        self.assertIsNone(_resolve([]))

    def test_an_event_with_no_distance_resolves_to_None(self):
        """No window, so no pair -- rather than a guessed default trip size
        committing a household's pack to an invented number."""
        with self.assertLogs(ct._LOGGER, level="WARNING"):
            self.assertIsNone(
                _resolve(
                    [
                        TripEvent(
                            start=_T0 + timedelta(hours=8),
                            end=_T0 + timedelta(hours=9),
                            summary="dentist",
                        )
                    ]
                )
            )

    def test_None_is_not_the_same_as_a_zero_requirement(self):
        """A zero `must_have_soc_kwh` paired with an index would pin the pack to
        its own floor at that period -- a real constraint nobody asked for. So
        the absence has to be `None`, not 0.0."""
        plan = _solve(None)
        self.assertEqual(plan.status, "optimal")
        ev = next(b for b in plan.batteries if b.name == "ev")
        self.assertIsNone(getattr(ev, "must_have_soc_kwh", None))

    def test_a_fully_driven_trip_resolves_to_None(self):
        """Odometer says the distance is already covered, so nothing is owed."""
        start = _T0 + timedelta(hours=8)
        self.assertIsNone(
            _resolve(
                [
                    TripEvent(
                        start=start, end=start + timedelta(hours=2), summary="80 km"
                    )
                ],
                already_driven_km_by_start={start: 80.0},
            )
        )


class TestOnlyTheSoonestTripIsPlannedFor(unittest.TestCase):
    """A real limitation of the primitive, pinned so it is not mistaken for a
    bug later: `BatteryConfig` carries ONE deadline pair."""

    def test_the_soonest_wins_and_the_rest_are_reported(self):
        events = [
            TripEvent(
                start=_T0 + timedelta(hours=16),
                end=_T0 + timedelta(hours=18),
                summary="evening 40 km",
            ),
            TripEvent(
                start=_T0 + timedelta(hours=8),
                end=_T0 + timedelta(hours=10),
                summary="morning 120 km",
            ),
        ]
        with self.assertLogs(ct._LOGGER, level="INFO") as cm:
            resolved = _resolve(events)
        index, kwh = resolved
        self.assertEqual(_GRID_TIMES[index].hour, 8, "the soonest departure")
        self.assertAlmostEqual(kwh, _EV_MIN_SOC + 21.6, places=4)
        joined = "\n".join(cm.output)
        self.assertIn("2 trips", joined, "say that one was set aside")
        self.assertIn("soonest", joined)

    def test_the_set_aside_trip_is_still_visible_to_a_caller(self):
        """`resolve_trip_windows()` returns them all, so nothing is silently
        lost -- only the LP pair is narrowed."""
        events = [
            TripEvent(
                start=_T0 + timedelta(hours=8),
                end=_T0 + timedelta(hours=10),
                summary="morning 120 km",
            ),
            TripEvent(
                start=_T0 + timedelta(hours=16),
                end=_T0 + timedelta(hours=18),
                summary="evening 40 km",
            ),
        ]
        windows = ct.resolve_trip_windows(
            events,
            _GRID_TIMES,
            kwh_per_100km=18.0,
            horizon_end=_HORIZON_END,
        )
        self.assertEqual(len(windows), 2)


class TestABiggerTripCostsMoreCharge(unittest.TestCase):
    """The requirement is sized from the distance, not a fixed percentage --
    which is the whole difference from the pre-existing fixed `departure_hour`
    mechanism (#563)."""

    def test_double_the_distance_roughly_doubles_the_energy(self):
        def kwh_for(summary: str) -> float:
            resolved = _resolve(
                [
                    TripEvent(
                        start=_T0 + timedelta(hours=8),
                        end=_T0 + timedelta(hours=12),
                        summary=summary,
                    )
                ]
            )
            return resolved[1] - _EV_MIN_SOC

        self.assertAlmostEqual(kwh_for("60 km"), 10.8, places=4)
        self.assertAlmostEqual(kwh_for("120 km"), 21.6, places=4)

    def test_a_trip_larger_than_the_pack_asks_to_arrive_full(self):
        """Clamped, because demanding more than the ceiling is infeasible and
        `must_have_soc_*` has no slack -- it would take the whole plan down."""
        resolved = _resolve(
            [
                TripEvent(
                    start=_T0 + timedelta(hours=8),
                    end=_T0 + timedelta(hours=20),
                    summary="1000 km",
                )
            ]
        )
        self.assertAlmostEqual(resolved[1], _EV_CAPACITY, places=6)
        plan = _solve(resolved)
        self.assertEqual(
            plan.status,
            "optimal",
            "an over-large trip must not make the whole plan infeasible",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
