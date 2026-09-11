"""nimbus issue #725 (Mark Purcell, live finding): a deferrable adequacy
load's own published 5-minute plan was jagged (0.65/0/0.40/0.40/0/0.29 kW
across consecutive periods) with nothing in the real economic signal
justifying it -- the exact same LP-degeneracy signature
test_solver_intraplan_smoothness.py already proved and fixed for the
battery/grid families (mechanism 4, `smoothness_weight`), which network.py
never applied to adequacy_vars at all until this fix.

Uses `adequacy_semi_continuous=False` (the continuous relaxation) since
the default (`True`) already forces every period hard 0-or-max -- nothing
continuous left to smooth -- matching Mark's own real load, which uses
the continuous mode (his own reported values include fractional kW).
"""

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _jaggedness(power_kw: np.ndarray) -> float:
    return float(np.sum(np.abs(np.diff(power_kw))))


def _disabled_battery() -> BatteryConfig:
    """build_plan() requires at least one BatteryConfig -- a physically
    disabled one (zero charge/discharge power, same convention
    test_solver_adequacy_semi_continuous.py already uses) isolates this
    test to the adequacy-load family alone."""
    return BatteryConfig(
        name="battery",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=20.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _flat_price_scenario():
    """12 five-minute periods, flat import/export price throughout --
    same real-numbers-not-invented discipline as the battery/grid
    version of this scenario. An adequacy load with a generous target
    and a wide window has zero real economic reason to prefer any one
    minute-by-minute delivery shape over another that sums to the same
    total -- classic degeneracy ground, same as the battery/grid case.
    """
    n = 12
    periods = PeriodGrid(
        hours=np.full(n, 5.0 / 60.0), start=datetime(2026, 9, 11, 7, 35, tzinfo=UTC)
    )
    grid = GridConfig(
        import_price=np.full(n, 0.0202),
        export_price=np.full(n, -0.0053),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 2.0))]
    adequacy_loads = [
        AdequacyLoadConfig(
            # Mark's own real load's max power. target_kwh sized to need
            # only ~4 of the 12 periods' worth of full-power energy --
            # genuine slack for the LP to spread delivery across any
            # subset of periods it likes, the same degeneracy shape as
            # the battery/grid flat-price scenario above.
            name="hws",
            max_power_kw=0.65,
            target_kwh=0.65 * (5.0 / 60.0) * 4,
            deadline_period=n - 1,
            shortfall_price=5.0,
            earliest_period=0,
        )
    ]
    return periods, grid, solar, loads, adequacy_loads


class TestAdequacyIntraplanSmoothness(unittest.TestCase):
    def test_jagged_delivery_is_reproducible_without_the_fix(self):
        periods, grid, solar, loads, adequacy_loads = _flat_price_scenario()
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery()],
            solar=solar,
            loads=loads,
            adequacy_loads=adequacy_loads,
            adequacy_semi_continuous=False,
            adequacy_earliness_budget_kw=0.0,
            smoothness_weight=0.0,
        )
        self.assertEqual(plan.status, "optimal")
        jag = _jaggedness(plan.adequacy_loads[0].power_kw)
        self.assertGreater(
            jag,
            0.5,
            "expected a genuinely jagged degenerate delivery pattern with the fix off",
        )

    def test_smoothness_weight_eliminates_jaggedness_at_zero_extra_cost(self):
        periods, grid, solar, loads, adequacy_loads = _flat_price_scenario()
        plan_off = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery()],
            solar=solar,
            loads=loads,
            adequacy_loads=adequacy_loads,
            adequacy_semi_continuous=False,
            adequacy_earliness_budget_kw=0.0,
            smoothness_weight=0.0,
        )
        plan_on = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery()],
            solar=solar,
            loads=loads,
            adequacy_loads=adequacy_loads,
            adequacy_semi_continuous=False,
            adequacy_earliness_budget_kw=0.0,
            smoothness_weight=0.005,
        )
        self.assertEqual(plan_on.status, "optimal")
        jag_on = _jaggedness(plan_on.adequacy_loads[0].power_kw)
        self.assertLess(
            jag_on,
            0.05,
            "smoothness_weight should flatten delivery to essentially one smooth level",
        )
        self.assertAlmostEqual(
            plan_on.total_cost,
            plan_off.total_cost,
            places=2,
            msg="a genuine degenerate tie must cost the same either way",
        )

    def test_default_semi_continuous_mode_is_unaffected(self):
        """The default (adequacy_semi_continuous=True) already forces
        every period hard 0-or-max -- confirms the new penalty doesn't
        break that existing, more common configuration."""
        periods, grid, solar, loads, adequacy_loads = _flat_price_scenario()
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery()],
            solar=solar,
            loads=loads,
            adequacy_loads=adequacy_loads,
            smoothness_weight=0.005,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        for v in power:
            self.assertTrue(
                v < 1e-6 or abs(v - 0.65) < 1e-6,
                f"expected hard 0-or-max under the default semi-continuous mode, got {v}",
            )


if __name__ == "__main__":
    unittest.main()
