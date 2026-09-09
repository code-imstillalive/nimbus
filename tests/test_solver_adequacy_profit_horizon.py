"""nimbus issue #482 (price-gated loads, e.g. a Bitcoin miner): network.
AdequacyLoadPlan.profit_horizon -- "what did the miner earn over what
its energy was worth", `sum((value_per_kwh[t] - lambda(t)) * power[t] *
hours[t])` across the whole solve horizon, where lambda(t) is the real
whole-system marginal cost of a kWh (the same power_balance_t{t} dual
#613's own shadow_price already exposes).
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 20.0,
        "initial_soc_kwh": 10.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 20.0,
        "max_charge_kw": 0.0,
        "max_discharge_kw": 0.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestProfitHorizonBasics(unittest.TestCase):
    def test_none_when_value_per_kwh_is_not_configured(self):
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.10),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertIsNone(plan.adequacy_loads[0].profit_horizon)

    def test_matches_hand_computed_value_for_a_simple_flat_scenario(self):
        # Battery/solar disabled -> grid import is the ONLY real marginal
        # source, so lambda(t) == import_price[t] exactly (same reasoning
        # TestAdequacyValuePerKwhPriceGating already establishes). Load
        # gates in below value=0.10 and runs its full 1.3 kWh target at
        # 0.05/kWh -- real profit = (0.10 - 0.05) * 1.3 = 0.065.
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.05),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                value_per_kwh=0.10,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        profit = plan.adequacy_loads[0].profit_horizon
        self.assertIsNotNone(profit)
        self.assertAlmostEqual(profit, 0.065, places=3)

    def test_a_load_priced_out_everywhere_earns_zero_not_a_fabricated_number(self):
        # value_per_kwh below the real marginal cost everywhere -- the
        # load never gates in at all (delivers via the priced shortfall
        # instead), so profit_horizon must be exactly 0.0 (nothing
        # credited, nothing run), not some nonzero artefact.
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=0.01,  # cheap enough that shedding wins
                value_per_kwh=0.05,  # well below the real 0.30 marginal cost
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertTrue(np.allclose(power, 0.0))
        self.assertAlmostEqual(plan.adequacy_loads[0].profit_horizon, 0.0, places=4)


class TestProfitHorizonWithWindows(unittest.TestCase):
    def test_sums_across_every_window_nimbus_issue_612(self):
        n = 48
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.05),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=16,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                value_per_kwh=0.10,
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=16, target_kwh=1.3
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=1.3
                    ),
                ),
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        # Both windows gate in fully (real margin 0.05/kWh each) -> 2x
        # the single-window profit from the flat-scenario test above.
        self.assertAlmostEqual(
            plan.adequacy_loads[0].profit_horizon, 0.065 * 2, places=3
        )


if __name__ == "__main__":
    unittest.main()
