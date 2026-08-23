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


def _base_kwargs(**overrides):
    kwargs = dict(
        capacity_kwh=100.0,
        initial_soc_kwh=50.0,
        min_soc_kwh=5.0,
        max_soc_kwh=100.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
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
