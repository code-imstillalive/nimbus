"""A soft battery reserve: kept unless a spike pays for it (#1654, #1658)
(#1837).
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

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

    def test_a_margin_too_small_sells_before_the_drop(self):
        # 5c is less than the 13.9c -> 6.9c drop, so selling early still wins:
        # the margin must cover the gap to the after-window price.
        prices = np.full(W_N, 0.139)
        prices[6:] = 0.069
        plan = _window_plan(_windowed(20.0, 15.0, 0.05, self.WINDOW), prices, 0.228)
        sold = float(np.sum(plan.grid_export_kw[:6] * W_HOURS[:6]))
        self.assertGreater(sold, 5.0)

    def test_an_empty_window_is_no_reserve(self):
        a = _window_plan(_battery(20.0), np.full(W_N, 0.139), 0.228)
        b = _window_plan(
            _windowed(20.0, 15.0, 0.20, frozenset()), np.full(W_N, 0.139), 0.228
        )
        np.testing.assert_allclose(a.batteries[0].soc_kwh, b.batteries[0].soc_kwh)


class TestReserveWindowAhead(unittest.TestCase):
    """A window that opens LATER in the plan (household, 20 Oct 2026): the
    first six periods are before it, the next six are the window. Entry used
    to be free, so the plan started `held` at 0 and the reserve did nothing
    in any window not already running when it was solved."""

    WINDOW = frozenset(range(6, 12))

    def _plan(self, initial, release, export_price, import_price=0.228):
        return _window_plan(
            _windowed(initial, 15.0, release, self.WINDOW),
            export_price,
            import_price,
        )

    def test_a_price_above_the_release_price_still_sells_it(self):
        # Four 5-minute periods at 36 kW sell 12 kWh: past the 5 kWh above
        # the reserve and into it. Grid energy at 60c, so the plan cannot
        # simply buy ahead (at 22.8c it does, and keeps the reserve whole).
        prices = np.full(W_N, 0.15)
        prices[7:11] = 0.88
        plan = self._plan(20.0, 0.30, prices, import_price=0.60)
        self.assertLess(float(min(plan.batteries[0].soc_kwh[6:])), 15.0 - 1.0)


class TestReserveWindowIndices(unittest.TestCase):
    """solver_plan.reserve_window_indices: local hours -> period indices."""

    def _times(self, start_hour, n, minutes=30):
        from solver_shared import LOCAL_TZ

        t0 = datetime(2026, 10, 10, start_hour, 0, tzinfo=LOCAL_TZ)
        return [t0 + timedelta(minutes=minutes * i) for i in range(n)]


if __name__ == "__main__":
    unittest.main()
