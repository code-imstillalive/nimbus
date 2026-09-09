"""nimbus issue #491 (Signals 2/7 of #489): grid-operator and per-battery
headroom/forced-cost signals, built on #490's own LPResult ranging.

`build_plan(..., compute_signals=True)` is required to get any of this
-- OFF by default. A real timing regression found while building this
(a 288-period battery-power-curve scenario went from ~0.6s to ~5.6s with
ranging on) means this can't be unconditional the way #490's own module
docstring hoped a plain LP's ranging pass would be; see build_plan()'s
own docstring for the full reasoning.

Every scenario below was run directly against the real solver first and
the assertions below match the REAL, verified output -- not a guess at
what HiGHS's ranging "should" say. Two real findings worth being
explicit about, since they differ from #491's own informal acceptance
note (a design-doc aside, not a literal spec):

- `grid_export_headroom_kw` is 0 only when solar surplus EXACTLY
  saturates the export cap (nothing curtailed, nothing left to give even
  if the cap were relaxed) -- a scenario with MORE surplus than the cap
  (e.g. 45 kW of real surplus against a 10 kW cap) correctly reports the
  real remaining headroom (35 kW: how far the cap could be relaxed
  before curtailment would start, or another constraint would bind),
  which is the actually useful "how far could an operator push this"
  signal the whole feature exists to answer -- reporting 0 there instead
  would be dishonest.
- `forced_export_cost` is NEGATIVE (not positive) when relaxing the
  export cap would be a net benefit to the plan (free extra revenue) --
  standard LP reduced-cost sign convention for a variable pinned at its
  own UPPER bound in a MINIMIZATION problem: relaxing an upper bound
  that's costing you money moves the objective DOWN, so its reduced
  cost is negative. `forced_import_cost`/`forced_export_cost` are a
  direct, unmodified pass-through of `reduced_costs[...] / hours[t]` --
  exactly the formula #491 itself specifies -- so this sign is inherited
  from that formula, not invented here.
"""

from __future__ import annotations

import unittest

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


def _grid(n: int, *, import_limit=50.0, export_limit=50.0) -> GridConfig:
    return GridConfig(
        import_price=np.full(n, 0.30),
        export_price=np.full(n, 0.10),
        import_limit_kw=import_limit,
        export_limit_kw=export_limit,
    )


def _pinned_battery(capacity: float = 10.0) -> BatteryConfig:
    # Fully pinned (min_soc == max_soc == capacity, initial == capacity)
    # -- genuinely zero charge/discharge headroom of its own, so it
    # never interferes with a grid-signal scenario that's deliberately
    # isolating the GRID side alone.
    return BatteryConfig(
        name="pinned",
        capacity_kwh=capacity,
        initial_soc_kwh=capacity,
        min_soc_kwh=capacity,
        max_soc_kwh=capacity,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


class TestComputeSignalsIsOptIn(unittest.TestCase):
    def test_signals_are_none_by_default(self):
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertIsNone(plan.grid_signals)
        # Physical battery-signal fields need no ranging at all -- still
        # populated even with compute_signals left at its default False.
        self.assertEqual(len(plan.battery_signals), 1)
        bs = plan.battery_signals[0]
        self.assertEqual(bs.name, "pinned")
        self.assertIsNotNone(bs.available_up_kw)
        self.assertIsNotNone(bs.available_down_kw)
        self.assertIsNone(bs.available_up_ranging_kw)
        self.assertIsNone(bs.available_down_ranging_kw)

    def test_a_normal_solve_with_compute_signals_gets_real_grid_signals(self):
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertIsNotNone(plan.grid_signals)
        self.assertEqual(len(plan.battery_signals), 1)
        self.assertEqual(plan.battery_signals[0].name, "pinned")
        self.assertIsNotNone(plan.battery_signals[0].available_up_ranging_kw)


class TestExportHeadroomAtExactSaturation(unittest.TestCase):
    def test_export_headroom_is_zero_when_surplus_exactly_matches_the_cap(self):
        # solar(15) - load(5) = 10 = export_limit exactly -- zero
        # curtailment, zero real surplus left even if the cap were
        # relaxed. Verified directly: grid_export_headroom_kw == 0.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n, export_limit=10.0),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.full(n, 15.0)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertTrue(np.all(plan.grid_export_kw == 10.0))
        self.assertTrue(np.all(plan.solar_curtailed_kw == 0.0))
        signals = plan.grid_signals
        self.assertIsNotNone(signals)
        np.testing.assert_allclose(signals.grid_export_headroom_kw, 0.0, atol=1e-6)
        np.testing.assert_allclose(signals.grid_export_headroom_kwh, 0.0, atol=1e-6)

    def test_export_headroom_is_real_and_positive_with_genuine_extra_surplus(self):
        # solar(50) - load(5) = 45 real surplus against a 10 kW cap --
        # 35 kW of real, usable headroom if the cap were relaxed.
        # Verified directly against the real solve.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n, export_limit=10.0),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.full(n, 50.0)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        signals = plan.grid_signals
        self.assertIsNotNone(signals)
        np.testing.assert_allclose(signals.grid_export_headroom_kw, 35.0, atol=1e-6)
        # Relaxing the export cap here is a genuine net benefit to the
        # plan (free extra revenue from surplus solar that would
        # otherwise be curtailed) -- reduced cost at an upper bound in a
        # minimization problem is negative when relaxing helps.
        np.testing.assert_allclose(signals.forced_export_cost, -0.10, atol=1e-6)
        self.assertTrue(np.all(signals.forced_export_cost < 0.0))
        # flex_available_down_kw mirrors grid_export_headroom_kw until
        # #493's own envelope lands (see GridSignals' own docstring).
        np.testing.assert_allclose(
            signals.flex_available_down_kw, signals.grid_export_headroom_kw
        )


class TestSwitchboardLoadHeadroom(unittest.TestCase):
    """nimbus issue #492 (Signals 3/7 of #489): how much MORE/LESS load
    this period could absorb before its own real-time price changes --
    RHS ranging on the SAME `power_balance_t{t}` row #491's
    `shadow_price` field already reads the dual of."""

    def test_load_headroom_matches_the_hand_worked_scenario(self):
        # 5 kW load, 50 kW import cap, fully pinned battery -- up=45
        # (room to grow before the import cap itself binds), down=5
        # (room to shrink to zero before the balance would need to
        # start exporting instead). Verified directly against the real
        # solve.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        signals = plan.grid_signals
        self.assertIsNotNone(signals)
        np.testing.assert_allclose(signals.load_headroom_up_kwh, 45.0, atol=1e-6)
        np.testing.assert_allclose(signals.load_headroom_down_kwh, 5.0, atol=1e-6)

    def test_load_headroom_shrinks_with_a_tighter_import_cap(self):
        # Same load, tighter 8 kW import cap -- up-headroom must shrink
        # to match (3 kW of room left before the now-tighter cap binds),
        # proving this genuinely tracks the real binding constraint
        # rather than returning a fixed/hardcoded number.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n, import_limit=8.0),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        signals = plan.grid_signals
        self.assertIsNotNone(signals)
        np.testing.assert_allclose(signals.load_headroom_up_kwh, 3.0, atol=1e-6)
        np.testing.assert_allclose(signals.load_headroom_down_kwh, 5.0, atol=1e-6)


class TestImportHeadroomWhenImportIsTheOnlyPath(unittest.TestCase):
    def test_import_headroom_is_zero_when_nothing_else_can_serve_the_load(self):
        # No solar, no battery available at all -- grid_import is pinned
        # to exactly load_kw by the power balance itself, with no other
        # variable free to absorb a relaxed cap. Verified directly:
        # grid_import_headroom_kw == 0, forced_import_cost == 0 (the
        # variable is fixed by an equality, not priced at a bound).
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        unavailable_battery = BatteryConfig(
            name="offline",
            capacity_kwh=100.0,
            initial_soc_kwh=50.0,
            min_soc_kwh=0.0,
            max_soc_kwh=100.0,
            max_charge_kw=20.0,
            max_discharge_kw=20.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
            available=False,
        )
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[unavailable_battery],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertTrue(np.all(plan.grid_import_kw == 5.0))
        signals = plan.grid_signals
        self.assertIsNotNone(signals)
        np.testing.assert_allclose(signals.grid_import_headroom_kw, 0.0, atol=1e-6)
        np.testing.assert_allclose(signals.forced_import_cost, 0.0, atol=1e-6)


class TestBatterySignals(unittest.TestCase):
    def test_ranging_headroom_never_exceeds_the_physical_headroom(self):
        # Real, verified scenario: a battery genuinely discharging at
        # different levels across 4 periods (17.5, 20, 5, 5 kW), some
        # against its own 20 kW max_discharge_kw cap, some not.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        battery = BatteryConfig(
            name="battery",
            capacity_kwh=100.0,
            initial_soc_kwh=50.0,
            min_soc_kwh=0.0,
            max_soc_kwh=100.0,
            max_charge_kw=20.0,
            max_discharge_kw=20.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
        )
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        bs = plan.battery_signals[0]
        self.assertEqual(bs.name, "battery")
        self.assertIsNotNone(bs.available_up_ranging_kw)
        self.assertIsNotNone(bs.available_down_ranging_kw)
        self.assertTrue(np.all(bs.available_up_ranging_kw <= bs.available_up_kw + 1e-9))
        self.assertTrue(
            np.all(bs.available_down_ranging_kw <= bs.available_down_kw + 1e-9)
        )
        # Real, hand-checkable numbers from the verified run -- not just
        # "some difference exists".
        np.testing.assert_allclose(
            bs.available_up_kw, [37.5, 40.0, 25.0, 25.0], atol=1e-6
        )
        np.testing.assert_allclose(
            bs.available_down_kw, [2.5, 0.0, 15.0, 15.0], atol=1e-6
        )

    def test_physical_headroom_is_always_populated_even_without_ranging(self):
        # available_up_kw/available_down_kw need no ranging at all (pure
        # arithmetic on the plan's own dispatch + the battery's own
        # config) -- always real, never None, regardless of ranging_valid.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[_pinned_battery()],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 5.0))],
        )
        bs = plan.battery_signals[0]
        self.assertIsNotNone(bs.available_up_kw)
        self.assertIsNotNone(bs.available_down_kw)


class TestSignalsAbsentOnNonOptimalPlan(unittest.TestCase):
    def test_grid_signals_is_none_and_battery_signals_is_empty_on_infeasible(self):
        # _infeasible_plan() is the exact same helper build_plan() itself
        # calls the instant result.status != "optimal" -- real code path,
        # not a hand-rolled stand-in (same helper test_main_infeasible_
        # plan_status_line.py already imports for the same reason).
        from solver.network import _infeasible_plan

        n = 2
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        infeasible_plan = _infeasible_plan(periods, "infeasible", iterations=25000)
        self.assertIsNone(infeasible_plan.grid_signals)
        self.assertEqual(infeasible_plan.battery_signals, [])


if __name__ == "__main__":
    unittest.main()
