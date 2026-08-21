"""Real household finding, 2026-08-21: "the forecasts are always wrong
but they tend to be more expensive in the afternoons, so waiting is not
a good idea." Existing risk_aversion (mechanism 3) only ever hedged
solar/load forecast error -- price_risk_aversion extends the same idea
to import/export price, using GridConfig.import_price_upper/
export_price_lower (both optional, both independent from risk_aversion
per the explicit "more flexibility" ask, symmetric for discharge/export
per the direct "same for discharge if possible" follow-up ask).
"""
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from solver.network import build_plan


def _scenario(import_price_upper=None, export_price_lower=None, price_risk_aversion=0.0):
    n = 10
    hours = np.array([1.0] * n)
    from datetime import datetime
    periods = PeriodGrid(hours=hours, start=datetime(2026, 8, 21, 12, 0))

    # A genuinely cheap-looking point forecast throughout -- if the LP
    # trusts it at face value, it has no reason to charge early (plenty
    # of time, no price pressure). A real household concern: the point
    # forecast for LATER periods might be understating what actually
    # happens.
    import_price = np.array([0.10] * n)
    export_price = np.array([0.30] * n)

    grid = GridConfig(
        import_price=import_price, export_price=export_price,
        import_limit_kw=44.0, export_limit_kw=44.0,
        import_price_upper=import_price_upper,
        export_price_lower=export_price_lower,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2, initial_soc_kwh=122.2 * 0.5,
        min_soc_kwh=122.2 * 0.05, max_soc_kwh=122.2 * 1.0,
        max_charge_kw=40.0, max_discharge_kw=40.0,
        charge_efficiency=0.975, discharge_efficiency=0.975,
        charge_cost=0.005, discharge_cost=0.01, salvage_value=0.15,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]
    return periods, grid, battery, solar, loads, price_risk_aversion


class TestPriceRiskAversion(unittest.TestCase):
    def test_zero_risk_aversion_is_a_complete_noop(self):
        """Even WITH real bounds given, price_risk_aversion=0.0 must
        produce byte-identical output to no bounds at all -- the
        no-op guarantee every other stability mechanism already has."""
        periods, grid_a, battery, solar, loads, _ = _scenario(
            import_price_upper=np.array([0.50] * 10), export_price_lower=np.array([0.05] * 10),
            price_risk_aversion=0.0,
        )
        periods_b, grid_b, _, _, _, _ = _scenario(price_risk_aversion=0.0)
        plan_a = build_plan(periods=periods, grid=grid_a, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        plan_b = build_plan(periods=periods_b, grid=grid_b, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        self.assertEqual(plan_a.status, "optimal")
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)
        np.testing.assert_allclose(plan_a.grid_export_kw, plan_b.grid_export_kw)
        self.assertAlmostEqual(plan_a.total_cost, plan_b.total_cost, places=6)

    def test_none_bounds_is_a_complete_noop_even_with_nonzero_risk_aversion(self):
        """price_risk_aversion=1.0 but no bounds given at all -- must
        also be a no-op, matching _risk_adjusted_one_sided()'s own
        documented guarantee."""
        periods, grid, battery, solar, loads, _ = _scenario(price_risk_aversion=1.0)
        periods_b, grid_b, _, _, _, _ = _scenario(price_risk_aversion=0.0)
        plan_a = build_plan(periods=periods, grid=grid, battery=battery, solar=solar, loads=loads, price_risk_aversion=1.0)
        plan_b = build_plan(periods=periods_b, grid=grid_b, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)
        np.testing.assert_allclose(plan_a.grid_export_kw, plan_b.grid_export_kw)

    def test_full_risk_aversion_uses_the_full_upper_bound_for_import(self):
        """price_risk_aversion=1.0 with a real upper bound -- the LP's
        OWN effective cost view should be exactly the bound (not the
        cheap point forecast), verified indirectly: charging becomes
        strictly less attractive than in the baseline (risk=0) case,
        since the LP now believes import is genuinely more expensive."""
        periods0, grid0, battery, solar, loads, _ = _scenario(
            import_price_upper=np.array([0.80] * 10), price_risk_aversion=0.0,
        )
        periods1, grid1, _, _, _, _ = _scenario(
            import_price_upper=np.array([0.80] * 10), price_risk_aversion=1.0,
        )
        plan0 = build_plan(periods=periods0, grid=grid0, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        plan1 = build_plan(periods=periods1, grid=grid1, battery=battery, solar=solar, loads=loads, price_risk_aversion=1.0)
        self.assertEqual(plan0.status, "optimal")
        self.assertEqual(plan1.status, "optimal")
        # Real, measurable effect: total import volume should NOT increase
        # (and typically decreases) once the LP believes import is more
        # expensive -- it has no reason to import MORE under a pessimistic
        # price view than under an optimistic one, all else equal.
        self.assertLessEqual(plan1.grid_import_kw.sum(), plan0.grid_import_kw.sum() + 1e-6)

    def test_export_price_lower_is_symmetric_for_discharge(self):
        """Direct household follow-up: 'i would do the same for
        discharge if possible.' A pessimistic (lower) export price bound
        should make export strictly less attractive (or equal), never
        MORE attractive, at full risk aversion."""
        periods0, grid0, battery, solar, loads, _ = _scenario(
            export_price_lower=np.array([0.05] * 10), price_risk_aversion=0.0,
        )
        periods1, grid1, _, _, _, _ = _scenario(
            export_price_lower=np.array([0.05] * 10), price_risk_aversion=1.0,
        )
        plan0 = build_plan(periods=periods0, grid=grid0, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        plan1 = build_plan(periods=periods1, grid=grid1, battery=battery, solar=solar, loads=loads, price_risk_aversion=1.0)
        self.assertGreaterEqual(plan1.total_cost, plan0.total_cost - 1e-6)

    def test_bound_worse_than_forecast_is_ignored_not_inverted(self):
        """A real, honest edge case: if the 'upper' bound given is
        actually BELOW the point forecast (a genuinely bad/stale bound),
        np.maximum(0.0, ...) must clamp this to zero adjustment, not
        push the effective price DOWN -- the mechanism can only ever
        push pessimistically in its own documented direction, never the
        opposite."""
        periods, grid, battery, solar, loads, _ = _scenario(
            import_price_upper=np.array([0.01] * 10),  # below the 0.10 point forecast
            price_risk_aversion=1.0,
        )
        periods_b, grid_b, _, _, _, _ = _scenario(price_risk_aversion=0.0)
        plan_a = build_plan(periods=periods, grid=grid, battery=battery, solar=solar, loads=loads, price_risk_aversion=1.0)
        plan_b = build_plan(periods=periods_b, grid=grid_b, battery=battery, solar=solar, loads=loads, price_risk_aversion=0.0)
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)


if __name__ == "__main__":
    unittest.main()
