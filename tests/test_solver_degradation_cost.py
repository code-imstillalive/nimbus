"""BatteryConfig.degradation_cost_per_kwh -- Track B2, the plan's own
long-flagged companion to charge_power_curve/discharge_power_curve
(Track B1, a POWER-LIMIT mechanism). This is a COST mechanism instead:
a real economic cycle-wear charge, $/kWh of throughput in either
direction, layered on top of whatever TOU-driven charge_cost/
discharge_cost already price -- see that field's own docstring in
elements.py for the full "why separate, not folded in" reasoning and
the real-world "(replacement cost) / (2 * capacity * rated EFC)"
derivation.

Confirmed absent everywhere before this, same day: regret.py's own
docstring explicitly excludes degradation from the objective,
nimbus_solver_forecast_writer.py's own equivalent_full_cycles is real
code but REPORTING-only (computed after the solve, never fed back into
what the LP optimizes against). This closes that gap.

These tests prove: (1) 0.0 (the default) is fully backward compatible;
(2) a negative value is rejected, not silently accepted; (3) a
MODERATE degradation cost that doesn't flip real arbitrage
profitability leaves the LP's DISPATCH decision unchanged but genuinely
raises total_cost by exactly the predicted amount -- proof the cost is
really being paid, not just present in the config; (4) a LARGE
degradation cost that exceeds the real arbitrage margin flips the LP's
decision to stop cycling almost entirely -- proof this can genuinely
discourage excessive cycling, the actual point of the feature, not
just a cosmetic addition.

Every expected numeric value below is hand-derived first, then
confirmed against a real local solve before being written down --
same discipline as test_solver_battery_power_curve.py.
"""
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid, SolarConfig
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    # initial_soc_kwh == min_soc_kwh -- deliberately starts EMPTY, not
    # mid-range. Confirmed live while building this test: a mid-range
    # starting SoC gives the LP "free" pre-existing stored energy (never
    # actually paid for) that it's profitable to just liquidate in EVERY
    # period regardless of price, completely swamping the genuine
    # round-trip (charge-then-discharge) behaviour this test needs to
    # isolate. Starting empty forces any real discharge to have been
    # funded by a real, priced charge earlier in the same horizon.
    defaults = dict(
        capacity_kwh=100.0, initial_soc_kwh=5.0, min_soc_kwh=5.0, max_soc_kwh=100.0,
        max_charge_kw=20.0, max_discharge_kw=20.0, charge_efficiency=0.95, discharge_efficiency=0.95,
        charge_cost=0.01, discharge_cost=0.02, salvage_value=0.0,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


# Real, deliberately large 2-period price spread -- cheap import in
# period 0, expensive export in period 1 -- so round-trip arbitrage is
# genuinely, unambiguously profitable before any degradation cost, and
# stays the dispatch driver being tested (not some other incidental
# constraint).
def _arbitrage_grid(n: int = 2) -> GridConfig:
    import_price = np.array([0.05, 0.40])
    export_price = np.array([0.05, 0.40])
    return GridConfig(import_price=import_price, export_price=export_price, import_limit_kw=50.0, export_limit_kw=50.0)


class TestBackwardCompatibility(unittest.TestCase):
    def test_default_zero_matches_explicit_zero(self):
        n = 2
        periods = _flat_grid(n)
        grid = _arbitrage_grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        plan_default = build_plan(periods=periods, grid=grid, battery=_base_battery(), solar=solar)
        plan_explicit = build_plan(periods=periods, grid=grid, battery=_base_battery(degradation_cost_per_kwh=0.0), solar=solar)
        self.assertEqual(plan_default.status, "optimal")
        self.assertAlmostEqual(plan_default.total_cost, plan_explicit.total_cost, places=6)
        for t in range(n):
            self.assertAlmostEqual(plan_default.battery_charge_kw[t], plan_explicit.battery_charge_kw[t], places=6)
            self.assertAlmostEqual(plan_default.battery_discharge_kw[t], plan_explicit.battery_discharge_kw[t], places=6)


class TestValidation(unittest.TestCase):
    def test_negative_degradation_cost_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(degradation_cost_per_kwh=-0.01)


class TestModerateDegradationCostPaidNotJustConfigured(unittest.TestCase):
    """Real arbitrage margin here is large (0.35/kWh export-import spread
    before efficiency/costs) -- a moderate degradation cost (0.05/kWh
    each direction) doesn't come close to flipping profitability, so the
    LP should still cycle the battery exactly as hard as it can (bang-
    bang at max_charge_kw/max_discharge_kw, same as with zero
    degradation) -- but total_cost should be measurably, exactly higher
    by (charge_kwh + discharge_kwh) * 0.05, proving the extra cost is
    genuinely being paid on real throughput, not just sitting unused in
    config."""

    def test_dispatch_unchanged_cost_increases_by_exact_amount(self):
        n = 2
        periods = _flat_grid(n)
        grid = _arbitrage_grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        plan_zero = build_plan(periods=periods, grid=grid, battery=_base_battery(), solar=solar)
        plan_moderate = build_plan(periods=periods, grid=grid, battery=_base_battery(degradation_cost_per_kwh=0.05), solar=solar)
        self.assertEqual(plan_zero.status, "optimal")
        self.assertEqual(plan_moderate.status, "optimal")

        total_charge_kwh = sum(plan_zero.battery_charge_kw)
        total_discharge_kwh = sum(plan_zero.battery_discharge_kw)
        # Real, confirmed dispatch: period 0 charges at the full max rate
        # (20kWh); period 1 discharges everything that charge actually
        # delivered into storage after round-trip efficiency loss --
        # 20 * 0.95 (charge_efficiency) * 0.95 (discharge_efficiency) =
        # 18.05kWh, bounded by the real round trip, not max_discharge_kw
        # (which would allow 20). Hand-derived, then confirmed against a
        # real local solve before being written down here.
        self.assertAlmostEqual(total_charge_kwh, 20.0, places=3)
        self.assertAlmostEqual(total_discharge_kwh, 18.05, places=3)

        # Same physical dispatch under moderate degradation -- proves the
        # cost didn't change WHAT the LP does here, only what it costs.
        for t in range(n):
            self.assertAlmostEqual(plan_zero.battery_charge_kw[t], plan_moderate.battery_charge_kw[t], places=3)
            self.assertAlmostEqual(plan_zero.battery_discharge_kw[t], plan_moderate.battery_discharge_kw[t], places=3)

        expected_extra_cost = (total_charge_kwh + total_discharge_kwh) * 0.05
        actual_extra_cost = plan_moderate.total_cost - plan_zero.total_cost
        self.assertAlmostEqual(actual_extra_cost, expected_extra_cost, places=3)
        self.assertGreater(actual_extra_cost, 0.0)  # a real, positive extra cost, not zero


class TestLargeDegradationCostStopsCycling(unittest.TestCase):
    """A degradation cost genuinely large enough to exceed the real
    arbitrage margin should flip the LP's own decision -- it should stop
    cycling almost entirely rather than pay a net loss to do so. This is
    the actual point of the feature: real cycle wear should be able to
    make excessive cycling genuinely uneconomic, not just reported after
    the fact."""

    def test_dispatch_collapses_when_degradation_exceeds_arbitrage_margin(self):
        n = 2
        periods = _flat_grid(n)
        grid = _arbitrage_grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        # 1.0/kWh each direction -- 2.0/kWh round trip, far exceeding the
        # real 0.35/kWh export-import spread even before efficiency loss.
        plan_large = build_plan(periods=periods, grid=grid, battery=_base_battery(degradation_cost_per_kwh=1.0), solar=solar)
        self.assertEqual(plan_large.status, "optimal")
        total_throughput = sum(plan_large.battery_charge_kw) + sum(plan_large.battery_discharge_kw)
        # Genuinely near zero, not just "reduced" -- confirms the cost is
        # large enough to make cycling a real net loss, not merely a
        # smaller-but-still-positive gain.
        self.assertLess(total_throughput, 1.0)


if __name__ == "__main__":
    unittest.main()
