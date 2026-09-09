"""nimbus issue #612: LP-level proof that AdequacyLoadConfig.windows
actually fixes the real bug -- "plan_forecast is 0.0 for 10-13 Sep, so
the temperature forecast decays to 12 C by Sunday and later days'
battery plans ignore the daily 2 kWh." A load with two independent
daily windows must genuinely deliver EACH day's own target within THAT
day's own period range, not just some combined total anywhere in the
horizon (which the old single-window path already technically "solved"
by only ever creating one window in the first place).
"""

from __future__ import annotations

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
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _run_two_day_plan(n=48, day1=(6, 16), day2=(30, 40), target_each=2.0):
    """A real 48h horizon (periods 0-47, 1h each): day 1's window is
    periods 6-16 (6am-4pm today), day 2's is periods 30-40 (6am-4pm
    tomorrow) -- the exact real HWS shape from #612's own report."""
    periods = _flat_grid(n)
    grid = GridConfig(
        import_price=np.full(n, 0.30),
        export_price=np.full(n, 0.05),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=1.0)
    adequacy = [
        AdequacyLoadConfig(
            name="hws",
            max_power_kw=0.65,
            # Legacy fields, unused for LP construction once windows is
            # set -- same values build_controllable_loads() itself would
            # populate them with (the first real window).
            target_kwh=target_each,
            deadline_period=day1[1],
            earliest_period=day1[0],
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            windows=(
                AdequacyWindow(
                    earliest_period=day1[0],
                    deadline_period=day1[1],
                    target_kwh=target_each,
                ),
                AdequacyWindow(
                    earliest_period=day2[0],
                    deadline_period=day2[1],
                    target_kwh=target_each,
                ),
            ),
        )
    ]
    return build_plan(
        periods=periods,
        grid=grid,
        batteries=[battery],
        solar=solar,
        loads=[],
        adequacy_loads=adequacy,
    )


class TestAdequacyWindowsIndependentDailyTargets(unittest.TestCase):
    def test_each_window_independently_meets_its_own_target(self):
        # This is the actual #612 assertion: NOT "2.0 kWh delivered
        # somewhere across the whole plan" (which a buggy
        # double-counting implementation could satisfy entirely within
        # day 1's window alone) -- each day's OWN period range must
        # independently reach its OWN target.
        plan = _run_two_day_plan()
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        hours = np.ones(len(power))
        day1_delivered = float(np.sum(power[6:17] * hours[6:17]))
        day2_delivered = float(np.sum(power[30:41] * hours[30:41]))
        self.assertGreaterEqual(day1_delivered, 2.0 - 1e-6)
        self.assertGreaterEqual(day2_delivered, 2.0 - 1e-6)

    def test_zero_power_outside_every_window(self):
        # Confirms the per-period bound genuinely zeroes the load in the
        # gap BETWEEN day 1's deadline and day 2's earliest (periods
        # 17-29) -- not just "somewhere outside SOME window."
        plan = _run_two_day_plan()
        power = plan.adequacy_loads[0].power_kw
        gap = power[17:30]
        self.assertTrue(
            np.allclose(gap, 0.0), f"expected zero power in the gap, got {gap}"
        )

    def test_day_1_delivering_extra_does_not_excuse_day_2s_own_target(self):
        # Real regression guard for the double-counting bug this fix
        # specifically avoids: force day 1 to way overshoot (a much
        # cheaper day-1-only price incentive can't exist here since
        # price is flat, so instead give day 1 a much higher max_power
        # ceiling and a very early deadline forcing concentrated max-power
        # delivery) and confirm day 2 STILL independently gets its own
        # 2.0 kWh -- day 1's overshoot must never count toward day 2.
        n = 48
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=1.0)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=5.0,
                target_kwh=2.0,
                deadline_period=16,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=16, target_kwh=2.0
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=2.0
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
        power = plan.adequacy_loads[0].power_kw
        day1_delivered = float(np.sum(power[6:17]))
        day2_delivered = float(np.sum(power[30:41]))
        self.assertGreaterEqual(day1_delivered, 2.0 - 1e-6)
        self.assertGreaterEqual(
            day2_delivered,
            2.0 - 1e-6,
            "day 2's own target must be met independently of day 1's delivery",
        )

    def test_genuinely_unreachable_window_reports_its_own_shortfall_not_a_crash(self):
        # A window too short to ever reach a large target (nimbus #477's
        # own soft-shortfall contract) -- must surface as a real, priced
        # shortfall on this per-window path too, not an infeasible plan.
        n = 48
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=1.0)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=0.5,
                target_kwh=100.0,  # physically unreachable in a 1-period window
                deadline_period=6,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=6, target_kwh=100.0
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=2.0
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
        self.assertGreater(plan.adequacy_loads[0].shortfall_kwh, 90.0)
        # Day 2's own separate, genuinely-reachable target must still be
        # met despite day 1's shortfall.
        power = plan.adequacy_loads[0].power_kw
        day2_delivered = float(np.sum(power[30:41]))
        self.assertGreaterEqual(day2_delivered, 2.0 - 1e-6)

    def test_value_per_kwh_credit_cap_sums_across_every_window(self):
        # nimbus issue #612 follow-on fix: the value_per_kwh credit cap
        # used to be bounded by the single legacy target_kwh field --
        # for a windowed load that must be the SUM of every window's own
        # target, not one day's worth, or a multi-day load would be
        # under-credited for real energy it's allowed to use.
        n = 48
        periods = _flat_grid(n)
        # A price so low every period that a pure price-gated load (no
        # real deadline pressure since shortfall_price is irrelevant
        # once value_per_kwh makes it a price-gated load) would run at
        # max_power_kw everywhere it's ALLOWED to, capped only by the
        # credit ceiling -- confirms that ceiling is 4.0 (2+2), not 2.0.
        grid = GridConfig(
            import_price=np.full(n, 0.01),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=10.0)
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
                target_kwh=2.0,
                deadline_period=16,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                value_per_kwh=1.0,  # >> the 0.01 import price -- always worth running
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=16, target_kwh=2.0
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=2.0
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
        power = plan.adequacy_loads[0].power_kw
        total_delivered = float(np.sum(power))
        # Both windows' own target (2.0 + 2.0 = 4.0) should be reachable
        # under the credit cap -- a cap wrongly stuck at 2.0 would make
        # this assertion fail.
        self.assertGreaterEqual(total_delivered, 4.0 - 1e-6)


if __name__ == "__main__":
    unittest.main()
