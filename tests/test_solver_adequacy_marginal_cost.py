"""nimbus issue #483 (sub-issue 7 of #476, "shadow costing per device"):
AdequacyLoadPlan.marginal_cost -- "what did each controllable load cost
to run", `sum(lambda(t) * power[t] * hours[t])` across the whole solve
horizon, where lambda(t) is the real whole-system marginal cost of a
kWh (the same power_balance_t{t} dual #613's own shadow_price already
exposes and #482's own profit_horizon already reads). Unlike
profit_horizon, this is computed unconditionally -- no value_per_kwh
needed for "what did this cost" to mean something.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
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


class TestMarginalCostBasics(unittest.TestCase):
    def test_never_none_even_without_value_per_kwh(self):
        # Same scenario TestProfitHorizonBasics uses for its own "None
        # without value_per_kwh" case -- marginal_cost has no such
        # gate, it's a real number regardless.
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
        al_plan = plan.adequacy_loads[0]
        self.assertIsNone(al_plan.profit_horizon)
        # Battery/solar disabled -> grid import is the only real
        # marginal source, so lambda(t) == import_price[t] == 0.10
        # exactly. Full 1.3 kWh target delivered: 1.3 * 0.10 = 0.13.
        self.assertAlmostEqual(al_plan.marginal_cost, 0.13, places=3)

    def test_matches_hand_computed_value_for_a_simple_flat_scenario(self):
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.07),
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
                value_per_kwh=0.20,
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
        al_plan = plan.adequacy_loads[0]
        # marginal_cost (pure lambda*p*dt) and profit_horizon
        # ((value-lambda)*p*dt) are genuinely different numbers here --
        # confirms marginal_cost isn't secretly just re-deriving profit.
        self.assertAlmostEqual(al_plan.marginal_cost, 1.3 * 0.07, places=3)
        self.assertAlmostEqual(al_plan.profit_horizon, 1.3 * (0.20 - 0.07), places=3)
        self.assertNotAlmostEqual(
            al_plan.marginal_cost, al_plan.profit_horizon, places=3
        )

    def test_near_zero_during_free_solar_surplus(self):
        # Abundant free solar, no load competing for it, battery
        # disabled and export capped at 0 so surplus solar has nowhere
        # to go but curtailment (zero shadow price) or this load --
        # real energy is delivered, but it was genuinely free at the
        # switchboard when it happened.
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=0.0,
        )
        solar = SolarConfig(forecast_kw=np.full(n, 10.0))
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
        al_plan = plan.adequacy_loads[0]
        self.assertAlmostEqual(al_plan.delivered_by_deadline_kwh, 1.3, places=3)
        self.assertAlmostEqual(al_plan.marginal_cost, 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
