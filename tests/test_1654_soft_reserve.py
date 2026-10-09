"""nimbus #1654 (9 Oct 2026 spike, #1658): a soft battery reserve.

On 9 Oct the reference household's plan sold the last ~9% of its battery at
28.6c at 04:25, thirty minutes before a $1.04-1.08 spike it had no forecast
for, and was empty through the spike. A reserve of R kWh with a release price
P means: the bottom R kWh is only sold when the price beats P. These are real
HiGHS solves.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

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


if __name__ == "__main__":
    unittest.main()
