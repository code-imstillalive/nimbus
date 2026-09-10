"""Direct test coverage for nimbus issue #696, Stage 2: build_plan()'s
new `solve_options` parameter, wiring the four existing tie-break
mechanisms (proximal_weight, smoothness_weight, adequacy_earliness_
budget_kw, battery_charge_earliness_budget_kw) onto Stage 1's real
primary/secondary objective architecture (`lp.py`'s `CalibratedOptions`
etc.) rather than leaving them hand-tuned primary-channel magnitudes.

Two real, load-bearing guarantees under test:

1. On a pure-LP scenario (no adequacy loads, or `adequacy_semi_
   continuous=False`), passing `solve_options=CalibratedOptions()`
   reproduces the EXACT SAME real-world guarantees the existing
   hand-tuned mechanism already had (a genuine degenerate burst gets
   eliminated at zero extra cost; a real, large price-driven transition
   is never smeared) -- proving the new architecture is a safe,
   equivalent replacement, not just "doesn't crash".
2. On a scenario that WILL be a MIP (adequacy loads present, semi-
   continuous default ON), passing `solve_options=CalibratedOptions()`
   does NOT raise `LPProblem`'s own MIP guard, and produces a result
   IDENTICAL to `solve_options=None` -- the documented, deliberate
   scope boundary (MIP + calibrated solve is "genuinely new ground",
   not yet designed) is a transparent, safe no-op for this call, never
   a crash on live production dispatch.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import CalibratedOptions
from solver.network import build_plan


def _jaggedness(discharge_kw: np.ndarray, charge_kw: np.ndarray) -> float:
    net = discharge_kw - charge_kw
    return float(np.sum(np.abs(np.diff(net))))


def _flat_price_scenario():
    """Same real household scenario as test_solver_intraplan_smoothness.py's
    own -- deliberately duplicated (not cross-imported) to keep this file
    self-contained, matching this project's own per-file test convention."""
    n = 12
    periods = PeriodGrid(
        hours=np.full(n, 5.0 / 60.0), start=datetime(2026, 8, 20, 13, 15, tzinfo=UTC)
    )
    grid = GridConfig(
        import_price=np.full(n, 0.0202),
        export_price=np.full(n, -0.0053),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=122.2,
        initial_soc_kwh=112.2,
        min_soc_kwh=2.44,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.926,
        discharge_efficiency=0.926,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(
        forecast_kw=np.full(n, 7.0), lower_kw=np.full(n, 6.0), upper_kw=np.full(n, 8.0)
    )
    loads = [
        LoadConfig(
            name="load",
            forecast_kw=np.full(n, 5.8),
            lower_kw=np.full(n, 5.0),
            upper_kw=np.full(n, 6.5),
        )
    ]
    return periods, grid, battery, solar, loads


def _price_step_scenario():
    n = 12
    periods = PeriodGrid(
        hours=np.full(n, 5.0 / 60.0), start=datetime(2026, 8, 20, 13, 15, tzinfo=UTC)
    )
    import_price = np.concatenate([np.full(6, 0.02), np.full(6, 0.50)])
    export_price = np.concatenate([np.full(6, -0.005), np.full(6, 0.45)])
    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=122.2,
        initial_soc_kwh=90.0,
        min_soc_kwh=2.44,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.926,
        discharge_efficiency=0.926,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(
        forecast_kw=np.full(n, 3.0), lower_kw=np.full(n, 2.5), upper_kw=np.full(n, 3.5)
    )
    loads = [
        LoadConfig(
            name="load",
            forecast_kw=np.full(n, 2.0),
            lower_kw=np.full(n, 1.5),
            upper_kw=np.full(n, 2.5),
        )
    ]
    return periods, grid, battery, solar, loads


class TestCalibratedOptionsPreservesSmoothnessGuarantees(unittest.TestCase):
    def test_burst_still_eliminated_at_zero_extra_cost(self):
        periods, grid, battery, solar, loads = _flat_price_scenario()
        plan_off = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=loads,
            smoothness_weight=0.0,
            battery_charge_earliness_budget_kw=0.0,
        )
        plan_on = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=loads,
            smoothness_weight=0.005,
            battery_charge_earliness_budget_kw=0.0,
            solve_options=CalibratedOptions(),
        )
        self.assertEqual(plan_on.status, "optimal")
        jag_on = _jaggedness(plan_on.battery_discharge_kw, plan_on.battery_charge_kw)
        self.assertLess(
            jag_on,
            1.0,
            "CalibratedOptions should still reduce the burst to essentially flat",
        )
        self.assertAlmostEqual(
            plan_on.total_cost,
            plan_off.total_cost,
            places=2,
            msg="a genuine degenerate tie must cost the same either way, "
            "even routed through the new secondary-channel architecture",
        )

    def test_genuine_price_step_still_not_smeared(self):
        periods, grid, battery, solar, loads = _price_step_scenario()
        plan_on = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=loads,
            smoothness_weight=0.005,
            battery_charge_earliness_budget_kw=0.0,
            solve_options=CalibratedOptions(),
        )
        self.assertEqual(plan_on.status, "optimal")
        net_on = plan_on.battery_discharge_kw - plan_on.battery_charge_kw
        step_on = abs(float(net_on[6] - net_on[5]))
        self.assertGreater(
            step_on,
            50.0,
            "CalibratedOptions must not smear a genuine, large, real transition",
        )


class TestMipFallbackIsTransparent(unittest.TestCase):
    """nimbus issue #696 Stage 2's own documented scope boundary (a MIP
    solve silently fell back to solve_options=None's own behavior)
    is now CLOSED by #702 -- a MIP genuinely engages solve_options
    instead of falling back, via lp.py's own two-phase-MIP pin-and-
    relax design (see _solve_with_options()'s own docstring). Class
    name kept for git-blame continuity; what it actually verifies now
    is that engaging the new architecture on a MIP never raises and
    never changes the real DISPATCH versus solve_options=None (only the
    reported total_cost legitimately differs, see the dispatch test's
    own comment for why)."""

    def _adequacy_scenario(self, *, adequacy_semi_continuous: bool = True):
        n = 8
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
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
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=3.0,
                target_kwh=6.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        return build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            adequacy_semi_continuous=adequacy_semi_continuous,
            solve_options=CalibratedOptions(),
        )

    def test_mip_scenario_with_calibrated_options_does_not_raise(self):
        plan = self._adequacy_scenario(adequacy_semi_continuous=True)
        self.assertEqual(plan.status, "optimal")

    def test_mip_scenario_matches_solve_options_none_dispatch(self):
        n = 8
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
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
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=3.0,
                target_kwh=6.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan_none = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            adequacy_semi_continuous=True,
            solve_options=None,
        )
        plan_calibrated = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            adequacy_semi_continuous=True,
            solve_options=CalibratedOptions(),
        )
        # nimbus issue #702: total_cost is deliberately NOT expected to
        # match exactly anymore. options=None bakes the earliness
        # tie-break directly into the reported primary cost for a MIP
        # (the pre-#702 fallback's own behavior); CalibratedOptions now
        # correctly routes it through the secondary channel instead, so
        # its own total_cost is the honest, uninflated true economic
        # cost -- always <= options=None's own, never more (removing an
        # always-nonnegative cost term can only lower or match the
        # total, never raise it).
        self.assertEqual(plan_none.status, plan_calibrated.status)
        self.assertLessEqual(plan_calibrated.total_cost, plan_none.total_cost + 1e-9)
        # What #702 actually exists to prove: the real DISPATCH -- which
        # periods the adequacy load actually runs in -- must still match
        # options=None's own (both should prefer the earliest tied
        # periods). A real, wrong first design attempt at #702 (pin
        # binaries to a primary-only MIP solve BEFORE ever reading
        # secondary cost) passed the old total_cost-only version of
        # this test while silently landing on an ARBITRARY tied
        # placement instead -- this assertion is what actually catches
        # that class of regression.
        np.testing.assert_allclose(
            plan_none.adequacy_loads[0].power_kw,
            plan_calibrated.adequacy_loads[0].power_kw,
        )

    def test_non_mip_adequacy_scenario_engages_the_new_architecture(self):
        """adequacy_semi_continuous=False turns the same scenario back
        into a pure LP -- solve_options=CalibratedOptions() must then
        actually run (no MIP fallback needed) and still produce a sane,
        optimal, earliness-preferring result."""
        plan = self._adequacy_scenario(adequacy_semi_continuous=False)
        self.assertEqual(plan.status, "optimal")
        self.assertGreater(plan.adequacy_loads[0].power_kw[0], 0.0)


if __name__ == "__main__":
    unittest.main()
