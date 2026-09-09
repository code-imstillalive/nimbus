"""nimbus issue #616: an AdequacyLoadConfig (deferrable load) is now
semi-continuous (power is EXACTLY 0 or EXACTLY max_power_kw every
period, never in between) and delivers as a single contiguous block
(turns on at most once per window) -- EMHASS's own treat_deferrable_
load_as_semi_cont + set_deferrable_load_single_constant, adopted per
#616's own real report: a 0.65 kW SG Ready heat pump planned as
`0.072, 0.384, 0.65, 0.65, 0.65, 0.403...` kW across consecutive
periods, a shape no real on/off contact can actually deliver, burning
a real daily activation on a plan artefact when the relay guard turned
it off between fractional bursts.
"""

from __future__ import annotations

import time
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


def _flat_price_scenario(n: int, import_price: float = 0.20):
    periods = _flat_grid(n)
    grid = GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, 0.0),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    battery = _base_battery()
    return periods, grid, solar, battery


def _is_zero_or(power: np.ndarray, level: float, atol: float = 1e-6) -> bool:
    return bool(
        np.all(np.isclose(power, 0.0, atol=atol) | np.isclose(power, level, atol=atol))
    )


def _count_blocks(power: np.ndarray, atol: float = 1e-6) -> int:
    """Number of contiguous runs of "on" (> atol) in a 1-D power series."""
    on = power > atol
    if not np.any(on):
        return 0
    transitions = np.diff(on.astype(int))
    return int(np.sum(transitions == 1)) + (1 if on[0] else 0)


class TestSemiContinuousDeliversExactlyOnOrOff(unittest.TestCase):
    def test_power_is_always_exactly_zero_or_exactly_max_power_kw(self):
        n = 24
        periods, grid, solar, battery = _flat_price_scenario(n)
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
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertTrue(
            _is_zero_or(power, 0.65),
            f"expected every period at 0 or 0.65, got {power}",
        )

    def test_a_real_fragmentation_scenario_now_delivers_one_clean_block(self):
        # Mark's own real shape (rescaled): a price dip right at the
        # boundary between two candidate periods used to be enough to
        # make the old continuous relaxation split delivery into several
        # short fractional bursts around it. Semi-continuous + single-
        # block must now deliver ONE contiguous run regardless.
        n = 12
        import_price = np.array(
            [0.30, 0.30, 0.05, 0.30, 0.30, 0.06, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30]
        )
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
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
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertEqual(
            _count_blocks(power),
            1,
            f"expected exactly one contiguous block, got {power}",
        )


class TestSemiContinuousOvershootsRatherThanFractionalize(unittest.TestCase):
    def test_a_target_not_a_clean_multiple_still_overshoots_not_fractionalizes(self):
        n = 24
        periods, grid, solar, battery = _flat_price_scenario(n)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.65,
                target_kwh=1.0,  # not a clean multiple of 0.65
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertTrue(_is_zero_or(power, 0.65))
        delivered = float(np.sum(power))  # 1h periods
        self.assertGreaterEqual(delivered, 1.0 - 1e-6)
        self.assertAlmostEqual(plan.adequacy_loads[0].shortfall_kwh, 0.0, places=4)


class TestSemiContinuousRespectsWindowsIndependently(unittest.TestCase):
    def test_each_window_gets_its_own_single_block(self):
        n = 48
        periods, grid, solar, battery = _flat_price_scenario(n)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=16,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
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
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertEqual(_count_blocks(power[6:17]), 1)
        self.assertEqual(_count_blocks(power[30:41]), 1)


class TestSemiContinuousCanBeDisabled(unittest.TestCase):
    def test_disabling_restores_the_pure_continuous_relaxation(self):
        n = 3
        import_price = np.array([0.03, 0.07, 0.15])
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
                target_kwh=6.0,
                earliest_period=0,
                deadline_period=2,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                value_per_kwh=0.10,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            adequacy_semi_continuous=False,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        self.assertAlmostEqual(power[0], 5.0, delta=1e-3)
        self.assertAlmostEqual(power[1], 1.0, delta=1e-3)


class TestSemiContinuousSolvesQuicklyAtRealisticScale(unittest.TestCase):
    def test_a_realistic_horizon_with_two_loads_solves_within_a_few_seconds(self):
        # This project's own real tiered grid is ~0-4 tier-0 (1-min) +
        # ~12 tier-1 (5-min) + ~190 tier-2 (30-min) periods for a full
        # 96h horizon (see solver_writer.py's own build_tiered_grid()) --
        # 200 flat 30-min periods here is a realistic, slightly
        # pessimistic stand-in (uniform periods are the WORSE case for a
        # MIP than the real tiered grid's mostly-coarse tail).
        n = 200
        periods, grid, solar, battery = _flat_price_scenario(n, import_price=0.20)
        grid = GridConfig(
            import_price=np.random.default_rng(0).uniform(0.05, 0.35, n),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.65,
                target_kwh=2.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            ),
            AdequacyLoadConfig(
                name="pool",
                max_power_kw=1.1,
                target_kwh=4.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            ),
        ]
        start = time.monotonic()
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(plan.status, "optimal")
        self.assertLess(elapsed, 10.0, f"MIP solve took {elapsed:.2f}s, expected < 10s")


class TestSemiContinuousAllowedMaskWithAGap(unittest.TestCase):
    def test_a_genuinely_non_contiguous_allowed_mask_still_picks_only_one_stretch(self):
        n = 24
        periods, grid, solar, battery = _flat_price_scenario(n)
        allowed = np.zeros(n, dtype=bool)
        allowed[2:6] = True  # first eligible stretch
        allowed[15:19] = True  # second eligible stretch, genuinely disjoint
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=n - 1,
                earliest_period=0,
                allowed=allowed,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = np.asarray(plan.adequacy_loads[0].power_kw)
        self.assertEqual(
            _count_blocks(power), 1, f"expected only one stretch used, got {power}"
        )
        # And confirm it stayed entirely within ONE of the two disjoint
        # eligible stretches, not spanning across the gap.
        used_in_first = np.any(power[2:6] > 1e-6)
        used_in_second = np.any(power[15:19] > 1e-6)
        self.assertNotEqual(used_in_first, used_in_second)


if __name__ == "__main__":
    unittest.main()
