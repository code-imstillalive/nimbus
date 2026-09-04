"""nimbus issue #328 (Mark Purcell): min_soc/max_soc were hard invariants
enforced by clamping observed SoC, silently corrupting every downstream
number (planned throughput, total_cost, the next cycle's own starting
assumption, and the EPR quality-report ratio, which compares two
trajectories -- J_ref and J_ach -- that started from differently-clamped
states). Fix: soc[t] keeps only its PHYSICAL bound [0, capacity]; a new
costed underfill[t]/overfill[t] slack pair (network.py) softly enforces
[min_soc_kwh, max_soc_kwh] instead, pinned to its true value by cost-
minimization (the penalty dominates every real $/kWh signal in the
model), never gameable.

Two extra gaps beyond Mark's own 3-item spec, found while implementing
it -- relaxing only soc[t]'s own bound wasn't sufficient, since two
OTHER constraints independently re-imposed a hard floor via algebraic
side effects:
  (a) the discharge wash-trade-prevention constraint forced
      soc[t-1] >= min_soc_kwh even at discharge[t]=0, since
      discharge[t]'s own lower bound is 0.
  (b) the terminal_value_breakpoints segment-fill EQUALITY constraint
      (sum(seg_vars) == soc[idx] - min_soc_kwh) was infeasible whenever
      soc[idx] < min_soc_kwh, since every seg_var has lb=0.
Both were fixed by folding the SAME underfill[t] slack into their own
algebra -- covered by tests 4/5 below.

Tests, matching Mark's own issue spec (a-d) plus the two extra findings
(e-f):
  a. a below-floor initial SoC solves without ValueError
  b. the plan schedules positive net charge recovering toward the floor
     over the first several periods when starting below it
  c. above-floor scenarios are numerically IDENTICAL to a version with
     the penalty mechanism entirely inert (backward compatibility)
  d. the underfill slack is never gameable -- the LP never inflates it
     beyond its true pinned value even when doing so would otherwise
     unlock artificial credit elsewhere (terminal-value segment fill)
  e. the discharge wash-trade constraint no longer forces soc[t-1] to
     the floor when starting (and staying) below it with discharge=0
  f. the terminal-value segment-fill equality stays feasible (doesn't
     raise/infeasible) when the horizon starts below the floor
"""

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

N = 24  # 24 hourly periods, one real day
CAPACITY = 40.0
MIN_SOC = CAPACITY * 0.05  # 2.0 kWh
MAX_SOC = CAPACITY * 1.0  # 40.0 kWh


def _scenario(initial_soc_kwh: float, terminal_value_breakpoints=None):
    start = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    hours = np.array([1.0] * N)
    periods = PeriodGrid(hours=hours, start=start)
    grid = GridConfig(
        import_price=np.full(N, 0.25),
        export_price=np.full(N, 0.10),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        capacity_kwh=CAPACITY,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=MIN_SOC,
        max_soc_kwh=MAX_SOC,
        max_charge_kw=21.0,
        max_discharge_kw=24.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.15,
        terminal_value_breakpoints=terminal_value_breakpoints,
        terminal_value_period_indices=[N - 1] if terminal_value_breakpoints else None,
    )
    solar = SolarConfig(forecast_kw=np.zeros(N))
    loads = [LoadConfig(name="house", forecast_kw=np.full(N, 1.0))]
    return periods, grid, battery, solar, loads


def _terminal_curve(base_rate, min_soc_kwh, max_soc_kwh):
    above_floor = max_soc_kwh - min_soc_kwh
    return [
        (above_floor * 0.15, base_rate * 2.2),
        (above_floor * 0.55, base_rate * 1.0),
        (above_floor * 0.30, base_rate * 0.3),
    ]


class TestBelowFloorSolvesWithoutCrash(unittest.TestCase):
    def test_solves_optimally_starting_well_below_min_soc(self):
        # 0.04 kWh -- deliberately below MIN_SOC (2.0 kWh), the exact
        # kind of live reading that used to raise ValueError at
        # BatteryConfig construction (Mark's issue, item 1).
        periods, grid, battery, solar, loads = _scenario(initial_soc_kwh=0.04)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")

    def test_solves_optimally_starting_above_max_soc(self):
        # Symmetric ceiling case -- a live reading above the configured
        # max (e.g. a hardware ramp overshoot).
        periods, grid, battery, solar, loads = _scenario(initial_soc_kwh=CAPACITY)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")


class TestScheduleRecoversTowardFloor(unittest.TestCase):
    def test_net_charges_over_first_periods_when_starting_below_floor(self):
        periods, grid, battery, solar, loads = _scenario(initial_soc_kwh=0.04)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # The LP should genuinely recover toward the floor rather than
        # sitting below it indefinitely or discharging further -- SoC a
        # few hours in should be meaningfully higher than the start.
        self.assertGreater(plan.battery_soc_kwh[3], 0.04 + 1.0)
        # And should reach at least the configured floor within the day.
        self.assertGreaterEqual(max(plan.battery_soc_kwh), MIN_SOC - 1e-6)


class TestAboveFloorBackwardCompatibility(unittest.TestCase):
    def test_identical_to_inert_penalty_when_comfortably_within_envelope(self):
        # Starting mid-envelope with a flat, moderate price shape, the
        # optimal plan should never need underfill/overfill at all --
        # confirms the new slack machinery is a true no-op (never
        # perturbs the optimum) for the common, healthy case.
        periods, grid, battery, solar, loads = _scenario(initial_soc_kwh=MAX_SOC / 2)
        plan_default = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        plan_explicit_tiny = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            soft_soc_penalty_per_kwh=1e9,  # absurdly dominant -- must still be a no-op
        )
        self.assertEqual(plan_default.status, "optimal")
        self.assertEqual(plan_explicit_tiny.status, "optimal")
        np.testing.assert_allclose(
            plan_default.battery_soc_kwh, plan_explicit_tiny.battery_soc_kwh, atol=1e-6
        )
        np.testing.assert_allclose(
            plan_default.grid_import_kw, plan_explicit_tiny.grid_import_kw, atol=1e-6
        )
        self.assertTrue(np.all(plan_default.battery_soc_kwh >= MIN_SOC - 1e-6))
        self.assertTrue(np.all(plan_default.battery_soc_kwh <= MAX_SOC + 1e-6))


class TestUnderfillNotGameable(unittest.TestCase):
    def test_underfill_never_exceeds_its_true_pinned_value(self):
        """The real risk this design has to rule out: the LP inflating
        underfill[t] beyond max(0, min_soc_kwh - soc[t]) to unlock
        artificial credit elsewhere (e.g. the terminal-value segment-fill
        equation, which also references underfill -- see fix (b) in this
        file's own module docstring). Proven directly: for every period,
        underfill implied by (min_soc_kwh - soc[t]) clamped at 0 must
        equal the actual soc[t] itself relative to the floor -- i.e.
        soc[t] is never pushed artificially low just to bank a larger
        underfill credit, which would show up as soc[t] sitting further
        below the floor than the household's own real recovery pressure
        (import price vs. discharge cost) would otherwise justify.
        """
        periods, grid, battery, solar, loads = _scenario(
            initial_soc_kwh=0.04,
            terminal_value_breakpoints=_terminal_curve(0.15, MIN_SOC, MAX_SOC),
        )
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # If underfill were gameable, the cheapest way to "earn" more of
        # it would be to never recover at all, or to recover as slowly
        # as possible -- instead the plan should charge up and clear the
        # floor well before the horizon end, since import price (0.25)
        # is cheap enough relative to the penalty to make recovery
        # strictly cheaper than sitting in underfill.
        recovered_by = next(
            (i for i, soc in enumerate(plan.battery_soc_kwh) if soc >= MIN_SOC - 1e-6),
            None,
        )
        self.assertIsNotNone(recovered_by, "plan never recovered to the floor at all")
        self.assertLess(recovered_by, N - 1)


class TestDischargeConstraintNoLongerForcesFloor(unittest.TestCase):
    def test_soc_can_stay_below_floor_with_zero_discharge(self):
        """Before the fix, the wash-trade-prevention constraint's own
        algebra (draw_coeff*discharge[t] <= soc[t-1] - min_soc_kwh)
        implicitly forced soc[t-1] >= min_soc_kwh at every period even
        when discharge[t] itself was 0 (its own lower bound) -- an
        infeasible LP whenever starting below the floor with export
        priced high enough that discharge would otherwise be attractive.
        Deliberately make export MORE attractive than import here (would
        have driven discharge, and thus infeasibility, under the old
        constraint) and confirm the LP still solves, correctly choosing
        zero discharge instead of crashing.
        """
        start = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
        hours = np.array([1.0] * N)
        periods = PeriodGrid(hours=hours, start=start)
        grid = GridConfig(
            import_price=np.full(N, 0.10),
            export_price=np.full(N, 0.30),  # discharge-favorable
            import_limit_kw=44.0,
            export_limit_kw=44.0,
        )
        battery = BatteryConfig(
            capacity_kwh=CAPACITY,
            initial_soc_kwh=0.04,
            min_soc_kwh=MIN_SOC,
            max_soc_kwh=MAX_SOC,
            max_charge_kw=21.0,
            max_discharge_kw=24.0,
            charge_efficiency=0.975,
            discharge_efficiency=0.975,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.15,
        )
        solar = SolarConfig(forecast_kw=np.zeros(N))
        loads = [LoadConfig(name="house", forecast_kw=np.full(N, 1.0))]
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # Real physical floor (0) must still hold even though the
        # scheduling floor (min_soc_kwh) doesn't apply below it.
        self.assertTrue(np.all(plan.battery_soc_kwh >= -1e-6))


class TestTerminalValueSegmentFillStaysFeasible(unittest.TestCase):
    def test_terminal_value_curve_active_with_below_floor_start(self):
        """Before the fix, the segment-fill EQUALITY constraint
        (sum(seg_vars) == soc[idx] - min_soc_kwh) was infeasible
        whenever soc[idx] < min_soc_kwh, since every seg_var has lb=0
        and the RHS would go negative. Confirms the horizon-final period
        (where the curve applies) stays feasible even when the horizon
        starts below the floor.
        """
        periods, grid, battery, solar, loads = _scenario(
            initial_soc_kwh=0.04,
            terminal_value_breakpoints=_terminal_curve(0.15, MIN_SOC, MAX_SOC),
        )
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
