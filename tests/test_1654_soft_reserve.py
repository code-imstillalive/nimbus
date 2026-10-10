"""nimbus #1654 (9 Oct 2026 spike, #1658): a soft battery reserve.

On 9 Oct the reference household's plan sold the last ~9% of its battery at
28.6c at 04:25, thirty minutes before a $1.04-1.08 spike it had no forecast
for, and was empty through the spike. A reserve of R kWh with a release price
P means: the bottom R kWh is only sold when the price beats P. These are real
HiGHS solves.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

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

N = 6
HOURS = np.full(N, 5 / 60)


def _battery(initial_soc: float, reserve: float = 0.0, release: float = 0.0):
    return BatteryConfig(
        name="home",
        capacity_kwh=100.0,
        initial_soc_kwh=initial_soc,
        min_soc_kwh=2.0,
        max_soc_kwh=100.0,
        max_charge_kw=36.0,
        max_discharge_kw=36.0,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.0,
        discharge_cost=0.01,
        salvage_value=0.05,  # a low value on energy left at the end
        reserve_kwh=reserve,
        reserve_release_price=release,
    )


def _plan(battery, export_price, import_price=0.39):
    return build_plan(
        periods=PeriodGrid(hours=HOURS, start=datetime(2026, 10, 9, 4, 25, tzinfo=UTC)),
        grid=GridConfig(
            import_price=np.full(N, import_price),
            export_price=np.asarray(export_price, dtype=float),
            import_limit_kw=60.0,
            export_limit_kw=40.0,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(N, 1.0))],
    )


def _sold_kwh(plan) -> float:
    return float(np.sum(plan.batteries[0].discharge_kw * HOURS))


class TestSoftReserve(unittest.TestCase):
    def test_without_a_reserve_the_battery_is_sold_at_an_ordinary_price(self):
        # The 9 Oct shape: 28.6c export, a low terminal value -> sell it all.
        plan = _plan(_battery(11.0), np.full(N, 0.286))
        self.assertEqual(plan.status, "optimal")
        self.assertGreater(_sold_kwh(plan), 8.0)

    def test_the_reserve_holds_at_a_price_below_its_release_price(self):
        plan = _plan(_battery(11.0, reserve=10.0, release=0.60), np.full(N, 0.286))
        self.assertEqual(plan.status, "optimal")
        # Only the ~1 kWh above the reserve (less the house's own use) goes.
        self.assertLessEqual(_sold_kwh(plan), 1.0 + 1e-6)
        self.assertGreaterEqual(float(plan.batteries[0].soc_kwh[-1]), 10.0 - 1e-6)

    def test_the_reserve_is_sold_into_a_price_above_its_release_price(self):
        prices = np.full(N, 0.286)
        prices[-2:] = 0.88  # the spike
        plan = _plan(_battery(11.0, reserve=10.0, release=0.60), prices)
        self.assertEqual(plan.status, "optimal")
        spike_kwh = float(np.sum(plan.batteries[0].discharge_kw[-2:] * HOURS[-2:]))
        self.assertGreater(spike_kwh, 5.0)

    def test_a_battery_below_the_reserve_is_not_forced_to_refill(self):
        plan = _plan(_battery(3.0, reserve=10.0, release=0.60), np.full(N, 0.286))
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(
            float(np.sum(plan.batteries[0].charge_kw)), 0.0, places=6
        )

    def test_off_by_default_the_plan_is_unchanged(self):
        base = _plan(_battery(11.0), np.full(N, 0.286))
        explicit = _plan(_battery(11.0, reserve=0.0, release=0.0), np.full(N, 0.286))
        np.testing.assert_allclose(
            base.batteries[0].discharge_kw, explicit.batteries[0].discharge_kw
        )
        np.testing.assert_allclose(
            base.batteries[0].soc_kwh, explicit.batteries[0].soc_kwh
        )


# ---- the daily window (#1654: "hold 15% through the pre-dawn hours, then
# sell it before the morning price drop") ---------------------------------

W_N = 12  # one hour of 5-minute periods
W_HOURS = np.full(W_N, 5 / 60)


def _window_plan(battery, export_price, import_price, load_kw=1.0):
    return build_plan(
        periods=PeriodGrid(
            hours=W_HOURS, start=datetime(2026, 10, 11, 5, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(W_N, import_price),
            export_price=np.asarray(export_price, dtype=float),
            import_limit_kw=60.0,
            export_limit_kw=40.0,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(W_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(W_N, load_kw))],
    )


def _windowed(initial_soc, reserve, release, window):
    return dataclasses.replace(
        _battery(initial_soc, reserve=reserve, release=release),
        reserve_period_indices=window,
    )


class TestReserveWindow(unittest.TestCase):
    """The first six periods are the window; the last six are after it."""

    WINDOW = range(6)

    def test_the_reserve_holds_inside_the_window(self):
        plan = _window_plan(
            _windowed(20.0, 15.0, 0.20, self.WINDOW), np.full(W_N, 0.139), 0.228
        )
        self.assertEqual(plan.status, "optimal")
        soc = plan.batteries[0].soc_kwh
        self.assertGreaterEqual(float(min(soc[:6])), 15.0 - 1e-6)

    def test_it_is_sold_after_the_window_without_paying_the_release_price(self):
        # 13.9c is below the 20c release price, yet once the window closes the
        # reserve sells: leaving the window releases it at no cost.
        plan = _window_plan(
            _windowed(20.0, 15.0, 0.20, self.WINDOW), np.full(W_N, 0.139), 0.228
        )
        after = float(np.sum(plan.batteries[0].discharge_kw[6:] * W_HOURS[6:]))
        self.assertGreater(after, 10.0)
        self.assertLess(float(plan.batteries[0].soc_kwh[-1]), 3.0)

    def test_a_spike_inside_the_window_is_sold_into(self):
        prices = np.full(W_N, 0.139)
        prices[3:5] = 0.88
        plan = _window_plan(_windowed(20.0, 15.0, 0.20, self.WINDOW), prices, 0.228)
        spike = float(np.sum(plan.batteries[0].discharge_kw[3:5] * W_HOURS[3:5]))
        self.assertGreater(spike, 5.0)

    def test_a_small_margin_keeps_the_house_on_the_battery(self):
        # The release price is a margin ON TOP of what the energy fetches
        # after the window (here 6.9c, 06:00 on 11 Oct 2026). 10c: selling at
        # 13.9c inside the window loses (13.5c < 10c + 6.7c later), while the
        # house's 22.8c import is beaten (10c + 6.7c < 22.8c), so the house
        # runs on the battery and nothing is bought from the grid.
        prices = np.full(W_N, 0.139)
        prices[6:] = 0.069
        plan = _window_plan(_windowed(15.0, 15.0, 0.10, self.WINDOW), prices, 0.228)
        self.assertEqual(plan.status, "optimal")
        imported = float(np.sum(plan.grid_import_kw[:6] * W_HOURS[:6]))
        sold_in_window = (
            float(np.sum(plan.batteries[0].discharge_kw[:6] * W_HOURS[:6]))
            - float(np.sum(np.full(6, 1.0) * W_HOURS[:6])) / 0.97
        )
        self.assertLess(imported, 1e-6)
        self.assertLess(sold_in_window, 1e-6)

    def test_a_margin_too_small_sells_before_the_drop(self):
        # 5c is less than the 13.9c -> 6.9c drop, so selling early still wins:
        # the margin must cover the gap to the after-window price.
        prices = np.full(W_N, 0.139)
        prices[6:] = 0.069
        plan = _window_plan(_windowed(20.0, 15.0, 0.05, self.WINDOW), prices, 0.228)
        sold = float(np.sum(plan.grid_export_kw[:6] * W_HOURS[:6]))
        self.assertGreater(sold, 5.0)

    def test_above_the_import_price_the_house_imports_instead(self):
        # Release 30c > import 22.8c: the reserve is held and the house's
        # overnight use is bought from the grid -- the trade-off to choose.
        plan = _window_plan(
            _windowed(15.0, 15.0, 0.30, self.WINDOW), np.full(W_N, 0.139), 0.228
        )
        imported = float(np.sum(plan.grid_import_kw[:6] * W_HOURS[:6]))
        self.assertGreater(imported, 0.4)

    def test_no_window_is_the_all_day_reserve(self):
        a = _window_plan(
            _battery(20.0, reserve=15.0, release=0.20), np.full(W_N, 0.139), 0.228
        )
        b = _window_plan(_windowed(20.0, 15.0, 0.20, None), np.full(W_N, 0.139), 0.228)
        np.testing.assert_allclose(a.batteries[0].soc_kwh, b.batteries[0].soc_kwh)
        self.assertGreaterEqual(float(min(a.batteries[0].soc_kwh)), 15.0 - 1e-6)

    def test_an_empty_window_is_no_reserve(self):
        a = _window_plan(_battery(20.0), np.full(W_N, 0.139), 0.228)
        b = _window_plan(
            _windowed(20.0, 15.0, 0.20, frozenset()), np.full(W_N, 0.139), 0.228
        )
        np.testing.assert_allclose(a.batteries[0].soc_kwh, b.batteries[0].soc_kwh)


class TestReserveWindowIndices(unittest.TestCase):
    """solver_plan.reserve_window_indices: local hours -> period indices."""

    def _times(self, start_hour, n, minutes=30):
        from solver_shared import LOCAL_TZ

        t0 = datetime(2026, 10, 10, start_hour, 0, tzinfo=LOCAL_TZ)
        return [t0 + timedelta(minutes=minutes * i) for i in range(n)]

    def test_equal_hours_mean_all_day(self):
        import solver_plan

        self.assertIsNone(solver_plan.reserve_window_indices(self._times(0, 4), 0, 0))
        self.assertIsNone(solver_plan.reserve_window_indices(self._times(0, 4), 3, 3))

    def test_a_pre_dawn_window(self):
        import solver_plan

        # 03:00, 03:30, ... 07:30 against 00:00-05:30
        got = solver_plan.reserve_window_indices(self._times(3, 10), 0, 5.5)
        self.assertEqual(got, frozenset({0, 1, 2, 3, 4}))

    def test_a_window_across_midnight(self):
        import solver_plan

        # 21:00, 22:00 ... 08:00 against 22:00-05:30
        times = self._times(21, 12, minutes=60)
        got = solver_plan.reserve_window_indices(times, 22, 5.5)
        hours = sorted(times[i].hour for i in got)
        self.assertEqual(hours, [0, 1, 2, 3, 4, 5, 22, 23])

    def test_utc_times_are_read_in_local_time(self):
        import solver_plan
        from solver_shared import LOCAL_TZ

        local = self._times(4, 2)
        utc = [t.astimezone(UTC) for t in local]
        self.assertEqual(
            solver_plan.reserve_window_indices(utc, 0, 5.5),
            solver_plan.reserve_window_indices(local, 0, 5.5),
        )
        self.assertEqual(str(LOCAL_TZ) != "UTC", True)


if __name__ == "__main__":
    unittest.main()
