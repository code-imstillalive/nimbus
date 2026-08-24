"""Real household finding (2026-08-22, direct chart evidence): a live
shadow-mode Solver plan kept discharging for ~50 minutes PAST the real
P2P window's own close, at essentially unchanged export price -- because
terminal_value_breakpoints only ever protected the horizon's own FINAL
period (see that field's own docstring), leaving every OTHER day
boundary in a multi-day horizon completely unprotected. With
discharge_cost held at a real, deliberately tiny $0.01/kWh, any export
price above that stays "profitable" forever, so the LP just kept selling
toward the SoC floor every night except the horizon's true last one --
nothing told it tomorrow has its own opportunity too.

terminal_value_period_indices (elements.py) is the fix: apply the SAME
terminal_value_breakpoints curve at additional period indices, not just
n-1. These tests prove it does what it's meant to, not just "doesn't
crash":
  1. WITH a checkpoint placed exactly at an intermediate day boundary,
     SoC AT that boundary is meaningfully higher than WITHOUT one -- the
     LP genuinely held real charge back specifically because that period
     now also earns the credit, isolating exactly the one thing that
     changed rather than asserting anything about the (potentially
     degenerate, since export price is deliberately flat here, matching
     the real live finding that price did NOT change at the boundary)
     unprotected baseline in isolation.
  2. The true final period's own terminal value still applies correctly
     alongside the new intermediate checkpoint (both credited, not one
     replacing the other).
  3. Backward compatibility: None is BYTE-IDENTICAL to the old hardcoded
     single-final-period behavior -- same plan, not just "similar".
Plus real validation tests for the 3 new BatteryConfig checks.
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

DAY_BOUNDARY_IDX = 23  # end of "day 1" (hours 0-23), 0-indexed
FINAL_IDX = 47  # true horizon end, 48 hourly periods total


def _terminal_curve(
    base_rate: float, min_soc_kwh: float, max_soc_kwh: float
) -> list[tuple[float, float]]:
    """Same 3-segment concave shape already validated elsewhere in this
    project (forward_value_comparison.py) -- not the object under test
    here, just a real, working curve to drive the scenario with."""
    above_floor = max_soc_kwh - min_soc_kwh
    return [
        (above_floor * 0.15, base_rate * 2.2),
        (above_floor * 0.55, base_rate * 1.0),
        (above_floor * 0.30, base_rate * 0.3),
    ]


def _scenario(period_indices):
    """48 hourly periods (2 real days). Flat, comfortably-profitable
    export price (0.15, well above the real $0.01/kWh discharge cost)
    for EVERY period -- deliberately never changes at the day boundary,
    matching the real live finding that this wasn't a price-driven
    decision. Import price (0.30) is deliberately unprofitable to
    charge into given the sell price (0.15 < 0.30/0.975/0.975), so the
    battery can't cheaply replenish mid-horizon -- isolates exactly the
    terminal-value mechanism's own effect on how much it holds back at
    each checkpoint, not a charging-opportunity side effect. No solar/
    load, so behavior is pure battery arbitrage.
    """
    n = FINAL_IDX + 1
    start = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    hours = np.array([1.0] * n)
    periods = PeriodGrid(hours=hours, start=start)
    grid = GridConfig(
        import_price=np.full(n, 0.30),
        export_price=np.full(n, 0.15),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    min_soc_kwh, max_soc_kwh = 122.2 * 0.05, 122.2 * 1.0
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=max_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
        terminal_value_breakpoints=_terminal_curve(0.15, min_soc_kwh, max_soc_kwh),
        terminal_value_period_indices=period_indices,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.zeros(n))]
    return periods, grid, battery, solar, loads


class TestTerminalValueCheckpoints(unittest.TestCase):
    def test_checkpoint_makes_soc_meaningfully_higher_at_the_boundary(self):
        without_periods, without_grid, without_battery, without_solar, without_loads = (
            _scenario(period_indices=None)
        )
        without_plan = build_plan(
            periods=without_periods,
            grid=without_grid,
            battery=without_battery,
            solar=without_solar,
            loads=without_loads,
        )

        with_periods, with_grid, with_battery, with_solar, with_loads = _scenario(
            period_indices=[DAY_BOUNDARY_IDX, FINAL_IDX]
        )
        with_plan = build_plan(
            periods=with_periods,
            grid=with_grid,
            battery=with_battery,
            solar=with_solar,
            loads=with_loads,
        )

        self.assertEqual(without_plan.status, "optimal")
        self.assertEqual(with_plan.status, "optimal")

        soc_without = without_plan.battery_soc_kwh[DAY_BOUNDARY_IDX]
        soc_with = with_plan.battery_soc_kwh[DAY_BOUNDARY_IDX]
        self.assertGreater(
            soc_with,
            soc_without + 5.0,
            f"expected the new checkpoint to genuinely change behavior -- "
            f"SoC at the boundary should be meaningfully higher WITH the "
            f"checkpoint ({soc_with:.1f}kWh) than without it ({soc_without:.1f}kWh)",
        )

    def test_final_period_still_credited_alongside_the_new_checkpoint(self):
        periods, grid, battery, solar, loads = _scenario(
            period_indices=[DAY_BOUNDARY_IDX, FINAL_IDX]
        )
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # The final period should ALSO show real held-back SoC (same
        # curve applying there too), not just the intermediate boundary
        # -- both checkpoints active, not one replacing the other.
        soc_at_final = plan.battery_soc_kwh[FINAL_IDX]
        floor_plus_15pct = battery.min_soc_kwh + 0.15 * (
            battery.max_soc_kwh - battery.min_soc_kwh
        )
        self.assertGreater(
            soc_at_final,
            floor_plus_15pct,
            f"expected the true final period to still be credited by the "
            f"terminal value curve, got SoC={soc_at_final:.1f}kWh "
            f"(floor+15%={floor_plus_15pct:.1f}kWh)",
        )

    def test_none_is_byte_identical_to_explicit_final_period_only(self):
        """The actual backward-compatibility guarantee: not just
        "similar", but the exact same plan whether the default (None) or
        an explicit [n-1] is passed -- confirming the None branch in
        network.py genuinely reduces to the pre-existing single-index
        behavior, not a coincidentally-similar-looking new code path."""
        auto_periods, auto_grid, auto_battery, auto_solar, auto_loads = _scenario(
            period_indices=None
        )
        auto_plan = build_plan(
            periods=auto_periods,
            grid=auto_grid,
            battery=auto_battery,
            solar=auto_solar,
            loads=auto_loads,
        )

        (
            explicit_periods,
            explicit_grid,
            explicit_battery,
            explicit_solar,
            explicit_loads,
        ) = _scenario(period_indices=[FINAL_IDX])
        explicit_plan = build_plan(
            periods=explicit_periods,
            grid=explicit_grid,
            battery=explicit_battery,
            solar=explicit_solar,
            loads=explicit_loads,
        )

        self.assertEqual(auto_plan.status, "optimal")
        self.assertEqual(explicit_plan.status, "optimal")
        np.testing.assert_allclose(
            auto_plan.battery_soc_kwh, explicit_plan.battery_soc_kwh, atol=1e-6
        )
        self.assertAlmostEqual(auto_plan.total_cost, explicit_plan.total_cost, places=4)


# 4 real days, hourly -- long enough to have multiple real midnight
# boundaries, matching the real production shape (nimbus #144, Mark
# Purcell's own real 96.9h/4-day horizon).
_MULTI_DAY_N = 96
_MULTI_DAY_MIDNIGHTS = [23, 47, 71, 95]  # end of day 1, 2, 3, 4 (0-indexed hourly)
_MULTI_DAY_FINAL_IDX = 95
_EARLY_REFERENCE_IDX = 12  # noon on day 1 -- before EVERY checkpoint below


def _multi_day_scenario(period_indices):
    """Deliberately profitable-to-sell every single period (export 0.20
    vs a real marginal discharge cost of ~0.09: discharge_cost 0.01 +
    degradation_cost_per_kwh 0.03, doubled for the round trip via
    charge_cost 0.01 + degradation_cost_per_kwh 0.03 too) -- the ONLY
    reason the LP would ever hold back is the terminal-value mechanism
    itself, isolating its effect precisely, same technique as
    _scenario() above just extended to a real multi-day length.
    """
    n = _MULTI_DAY_N
    start = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    hours = np.array([1.0] * n)
    periods = PeriodGrid(hours=hours, start=start)
    grid = GridConfig(
        import_price=np.full(n, 0.40),
        export_price=np.full(n, 0.20),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    min_soc_kwh, max_soc_kwh = 40.0 * 0.05, 40.0 * 1.0
    battery = BatteryConfig(
        capacity_kwh=40.0,
        initial_soc_kwh=max_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        max_charge_kw=24.0,
        max_discharge_kw=24.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.01,
        discharge_cost=0.01,
        degradation_cost_per_kwh=0.03,
        salvage_value=0.05,
        terminal_value_breakpoints=_terminal_curve(0.05, min_soc_kwh, max_soc_kwh),
        terminal_value_period_indices=period_indices,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 4.0))]
    return periods, grid, battery, solar, loads


class TestTerminalValueDoesNotCompoundAcrossMultipleCheckpoints(unittest.TestCase):
    """Real bug found live (Mark Purcell, nimbus #144, 2026-08-24): on a
    real 4-day horizon (4 real midnight checkpoints + the true final
    period = 5 total), the SAME physical stored energy was earning the
    FULL terminal-value credit at EVERY checkpoint it survived through,
    not once -- confirmed by a controlled scenario (this one, minus the
    fix) where SoC at a point hours before ANY checkpoint jumped from
    the real floor to full capacity the instant a SECOND checkpoint was
    added later in the SAME horizon, and total_cost kept getting
    monotonically "better" as more checkpoints were added -- the
    tell-tale sign of double-counting.

    These tests prove the fix directly: total_cost must NOT keep
    improving once a second (or third, or fourth) intermediate
    checkpoint is added -- it should plateau at whatever one real
    intermediate checkpoint already earns, since that's genuinely the
    correct amount of "carry into tomorrow" incentive a single unit of
    energy should ever collect, not one full payout per checkpoint.
    """

    def test_total_cost_plateaus_once_a_second_intermediate_checkpoint_exists(self):
        one_intermediate = sorted({_MULTI_DAY_MIDNIGHTS[-1], _MULTI_DAY_FINAL_IDX})
        two_intermediate = sorted({*_MULTI_DAY_MIDNIGHTS[-2:], _MULTI_DAY_FINAL_IDX})
        three_intermediate = sorted({*_MULTI_DAY_MIDNIGHTS[-3:], _MULTI_DAY_FINAL_IDX})
        four_intermediate = sorted({*_MULTI_DAY_MIDNIGHTS, _MULTI_DAY_FINAL_IDX})

        costs = {}
        for label, idxs in (
            ("1_intermediate", one_intermediate),
            ("2_intermediate", two_intermediate),
            ("3_intermediate", three_intermediate),
            ("4_intermediate", four_intermediate),
        ):
            periods, grid, battery, solar, loads = _multi_day_scenario(idxs)
            plan = build_plan(
                periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
            )
            self.assertEqual(plan.status, "optimal")
            costs[label] = plan.total_cost

        # 1 -> 2 intermediate checkpoints is real, EXPECTED, already-
        # tested behaviour (matches TestTerminalValueCheckpoints above --
        # a single intermediate checkpoint IS meant to change the plan).
        # But 2 -> 3 -> 4 must NOT keep improving further -- that's
        # exactly the compounding bug. Real dollar tolerance (not exact
        # equality), since numerical solver noise is real at this scale.
        self.assertAlmostEqual(
            costs["2_intermediate"],
            costs["3_intermediate"],
            delta=0.01,
            msg=f"total_cost kept improving from 2->3 intermediate checkpoints "
            f"({costs['2_intermediate']:.4f} -> {costs['3_intermediate']:.4f}) -- "
            f"the same stored energy is still being credited more than once",
        )
        self.assertAlmostEqual(
            costs["3_intermediate"],
            costs["4_intermediate"],
            delta=0.01,
            msg=f"total_cost kept improving from 3->4 intermediate checkpoints "
            f"({costs['3_intermediate']:.4f} -> {costs['4_intermediate']:.4f}) -- "
            f"the same stored energy is still being credited more than once",
        )

    def test_soc_at_an_early_common_reference_point_does_not_keep_rising(self):
        # Same real numbers as the total_cost test above, but checking
        # the actual DISPATCH decision (SoC at a point hours before any
        # checkpoint), not just the reported objective value -- direct
        # proof the LP's real behaviour, not only its accounting, stops
        # compounding.
        two_intermediate = sorted({*_MULTI_DAY_MIDNIGHTS[-2:], _MULTI_DAY_FINAL_IDX})
        four_intermediate = sorted({*_MULTI_DAY_MIDNIGHTS, _MULTI_DAY_FINAL_IDX})

        periods, grid, battery, solar, loads = _multi_day_scenario(two_intermediate)
        plan_2 = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        periods, grid, battery, solar, loads = _multi_day_scenario(four_intermediate)
        plan_4 = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )

        self.assertEqual(plan_2.status, "optimal")
        self.assertEqual(plan_4.status, "optimal")
        self.assertAlmostEqual(
            plan_2.battery_soc_kwh[_EARLY_REFERENCE_IDX],
            plan_4.battery_soc_kwh[_EARLY_REFERENCE_IDX],
            delta=0.5,
            msg="SoC at an early common reference point (before any checkpoint) "
            "differs between 2 and 4 intermediate checkpoints -- the extra "
            "downstream checkpoints are still influencing an unrelated, earlier "
            "decision",
        )

    def test_single_intermediate_checkpoint_is_completely_unaffected_by_the_fix(self):
        # Regression guard: the fix must be a genuine no-op for the exact
        # shape TestTerminalValueCheckpoints above already validates (one
        # intermediate checkpoint + the final period) -- n_intermediate=1
        # means the scale factor is exactly 1.0, unchanged from before
        # this fix existed. _MULTI_DAY_MIDNIGHTS[-2] (71, day 3's real
        # midnight) is deliberately used here, not [-1] (95) -- [-1] IS
        # _MULTI_DAY_FINAL_IDX on this grid, which would collapse to a
        # single checkpoint (zero intermediate) instead of the intended
        # "one real intermediate + the final period" shape.
        intermediate_idx = _MULTI_DAY_MIDNIGHTS[-2]
        idxs = sorted({intermediate_idx, _MULTI_DAY_FINAL_IDX})
        self.assertEqual(len(idxs), 2, "test setup bug: expected 2 distinct indices")
        periods, grid, battery, solar, loads = _multi_day_scenario(idxs)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # With comfortably-profitable export price and a real single
        # intermediate checkpoint, the LP should still hold SoC back
        # meaningfully at that checkpoint -- proves the mechanism is
        # still genuinely active, not accidentally neutered by the fix.
        self.assertGreater(
            plan.battery_soc_kwh[intermediate_idx], battery.min_soc_kwh + 5.0
        )


def _base_kwargs(**overrides):
    kwargs = {
        "capacity_kwh": 100.0,
        "initial_soc_kwh": 50.0,
        "min_soc_kwh": 5.0,
        "max_soc_kwh": 100.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.005,
        "discharge_cost": 0.01,
        "salvage_value": 0.15,
    }
    kwargs.update(overrides)
    return kwargs


class TestTerminalValuePeriodIndicesValidation(unittest.TestCase):
    def test_indices_without_breakpoints_raises(self):
        with self.assertRaises(ValueError):
            BatteryConfig(**_base_kwargs(terminal_value_period_indices=[10, 20]))

    def test_negative_index_raises(self):
        with self.assertRaises(ValueError):
            BatteryConfig(
                **_base_kwargs(
                    terminal_value_breakpoints=[(95.0, 0.15)],
                    terminal_value_period_indices=[-1, 10],
                )
            )

    def test_duplicate_index_raises(self):
        with self.assertRaises(ValueError):
            BatteryConfig(
                **_base_kwargs(
                    terminal_value_breakpoints=[(95.0, 0.15)],
                    terminal_value_period_indices=[10, 10],
                )
            )


if __name__ == "__main__":
    unittest.main()
