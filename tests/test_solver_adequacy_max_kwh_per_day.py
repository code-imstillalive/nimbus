"""nimbus issue #482 (price-gated loads, e.g. a Bitcoin miner):
AdequacyLoadConfig.max_kwh_per_day -- a real cumulative energy cap
applied SEPARATELY PER REAL CALENDAR DAY, not once across the whole
horizon (same reasoning, and the same network.py implementation
pattern, as GridConfig's own export_bonus_volume_kwh two-tier cap --
see p2p_export.add_export_bonus_cumulative_caps()'s own docstring for
the "why per-day not global" story this mirrors).

Scenario: a 2-real-day horizon, day 1 cheap, day 2 expensive, a real
deadline extending through day 2, and a shortfall price high enough
that the LP genuinely wants to reach its target as early (cheaply) as
possible. Without max_kwh_per_day the LP would deliver the whole
target on day 1 alone. With a tight max_kwh_per_day, day 1 must stay
under the cap and day 2 (expensive) is forced to make up the rest --
proving the cap is a real, binding constraint, not just a coincidence
of what the LP would have chosen anyway.
"""

from __future__ import annotations

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


def _two_day_scenario(*, start=None):
    n = 48  # 2 real days, 1-hour periods
    periods = PeriodGrid(hours=np.full(n, 1.0), start=start)
    import_price = np.concatenate([np.full(24, 0.05), np.full(24, 0.50)])
    grid = GridConfig(
        import_price=import_price,
        export_price=np.zeros(n),
        import_limit_kw=50.0,
        export_limit_kw=0.0,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=1.0,
        initial_soc_kwh=0.5,
        min_soc_kwh=0.0,
        max_soc_kwh=1.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.zeros(n))]
    return periods, grid, battery, solar, loads


class TestAdequacyMaxKwhPerDay(unittest.TestCase):
    def test_cap_binds_per_real_calendar_day_forcing_day2_delivery(self):
        periods, grid, battery, solar, loads = _two_day_scenario(
            start=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
        )
        # `adequacy_semi_continuous` is ON by default (every real
        # Controllable Load in this project is on/off dispatched, #616):
        # each "on" period runs at EXACTLY max_power_kw, never a partial
        # amount. The cap must be sized to a whole number of such blocks
        # (10.0 = exactly one 1-hour block at 10kW) for a load to be
        # able to deliver anything at all under it; target_kwh=20.0 (two
        # full blocks) forces a genuine two-block, cross-day delivery
        # once each day is independently capped at one block.
        miner = AdequacyLoadConfig(
            name="miner",
            max_power_kw=10.0,
            target_kwh=20.0,
            deadline_period=47,
            earliest_period=0,
            shortfall_price=5.0,
            max_kwh_per_day=10.0,
        )
        plan = build_plan(
            periods=periods,
            batteries=[battery],
            solar=solar,
            loads=loads,
            grid=grid,
            adequacy_loads=[miner],
        )
        self.assertEqual(plan.status, "optimal")
        al_plan = next(a for a in plan.adequacy_loads if a.name == "miner")
        power = np.asarray(al_plan.power_kw)
        day1_kwh = float(np.sum(power[:24]))
        day2_kwh = float(np.sum(power[24:]))
        # Day 1 must respect the cap despite being far cheaper -- without
        # it, a single 2-hour block would deliver the whole 20kWh target
        # on day 1 alone (see test_none_is_a_complete_no_op below).
        self.assertLessEqual(day1_kwh, 10.0 + 1e-6)
        self.assertLessEqual(day2_kwh, 10.0 + 1e-6)
        # The real target still needs reaching by the deadline (day 2's
        # own end) -- day 2 must pick up genuinely real delivery to get
        # there, even though it's 10x the price of day 1.
        self.assertGreater(day2_kwh, 5.0)
        self.assertAlmostEqual(day1_kwh + day2_kwh, 20.0, places=3)
        self.assertAlmostEqual(al_plan.shortfall_kwh, 0.0, places=3)

    def test_global_fallback_caps_the_whole_horizon_when_no_calendar_anchor(self):
        periods, grid, battery, solar, loads = _two_day_scenario(start=None)
        miner = AdequacyLoadConfig(
            name="miner",
            max_power_kw=10.0,
            target_kwh=20.0,
            deadline_period=47,
            earliest_period=0,
            shortfall_price=5.0,
            max_kwh_per_day=10.0,
        )
        plan = build_plan(
            periods=periods,
            batteries=[battery],
            solar=solar,
            loads=loads,
            grid=grid,
            adequacy_loads=[miner],
        )
        self.assertEqual(plan.status, "optimal")
        al_plan = next(a for a in plan.adequacy_loads if a.name == "miner")
        power = np.asarray(al_plan.power_kw)
        total_kwh = float(np.sum(power))
        # No calendar anchor -- one global cap across the WHOLE 48h
        # horizon, not 10kWh per real day. Since the cap (10.0) is below
        # the real target (20.0), the LP accepts a real shortfall
        # instead of ever exceeding the single global cap.
        self.assertLessEqual(total_kwh, 10.0 + 1e-6)
        self.assertGreater(al_plan.shortfall_kwh, 0.0)

    def test_none_is_a_complete_no_op(self):
        periods, grid, battery, solar, loads = _two_day_scenario(
            start=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
        )
        miner = AdequacyLoadConfig(
            name="miner",
            max_power_kw=10.0,
            target_kwh=20.0,
            deadline_period=47,
            earliest_period=0,
            shortfall_price=5.0,
            max_kwh_per_day=None,
        )
        plan = build_plan(
            periods=periods,
            batteries=[battery],
            solar=solar,
            loads=loads,
            grid=grid,
            adequacy_loads=[miner],
        )
        self.assertEqual(plan.status, "optimal")
        al_plan = next(a for a in plan.adequacy_loads if a.name == "miner")
        power = np.asarray(al_plan.power_kw)
        # Unconstrained: the whole cheap target is delivered on day 1
        # alone (one contiguous 2-hour block, #616's own single-block
        # rule), none of it deferred to the expensive day 2.
        self.assertAlmostEqual(float(np.sum(power[:24])), 20.0, places=3)
        self.assertAlmostEqual(float(np.sum(power[24:])), 0.0, places=3)

    def test_validation_rejects_non_positive_max_kwh_per_day(self):
        with self.assertRaises(ValueError):
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=10.0,
                target_kwh=15.0,
                deadline_period=10,
                shortfall_price=1.0,
                max_kwh_per_day=0.0,
            )
        with self.assertRaises(ValueError):
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=10.0,
                target_kwh=15.0,
                deadline_period=10,
                shortfall_price=1.0,
                max_kwh_per_day=-2.0,
            )


if __name__ == "__main__":
    unittest.main()
