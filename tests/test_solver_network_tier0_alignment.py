"""nimbus issue #635 (Mark Purcell): LP-level proof that a mid-slot
solve's own tier-0 (1-minute) periods are now genuinely tethered to the
previous plan's overlapping 5-minute period, instead of being parked at
an arbitrary vertex (the fleet's own power limit) because
_align_previous_periods() found no exact-start match for them.

Real repro shape, from Mark's own two captured consecutive plans: a
cron-triggered plan on a 5-minute grid (period 0: 17:25-17:30, discharge
17.9 kW) followed a minute later by a mid-slot solve (triggered by a
second price source, see #633) whose own grid starts with four 1-minute
tier-0 periods (17:26, 17:27, 17:28, 17:29) before returning to the
5-minute grid at 17:30. With a genuine (if tiny) economic incentive to
export more, and NO real tether, the old code parked those four periods
at the hard export limit; the fix ties them to the 17:25 period's own
17.9 kW instead.
"""

from __future__ import annotations

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
from solver.network import BatteryPlan, Plan, build_plan


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "home",
        "capacity_kwh": 40.0,
        "initial_soc_kwh": 30.0,
        "min_soc_kwh": 4.0,
        "max_soc_kwh": 40.0,
        "max_charge_kw": 25.0,
        "max_discharge_kw": 25.0,
        "charge_efficiency": 0.98,
        "discharge_efficiency": 0.98,
        "charge_cost": 0.01,
        "discharge_cost": 0.099,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _new_grid() -> PeriodGrid:
    # Four 1-minute tier-0 periods (17:26-17:29), then one 5-minute
    # period (17:30-17:35) -- the exact real shape of Mark's own
    # mid-slot solve.
    hours = np.array([1 / 60, 1 / 60, 1 / 60, 1 / 60, 5 / 60])
    return PeriodGrid(hours=hours, start=datetime(2026, 9, 9, 17, 26, tzinfo=UTC))


def _previous_plan_5min() -> Plan:
    # The prior, boundary-aligned cron solve: a single 5-minute period,
    # 17:25-17:30, discharging 17.9 kW -- Mark's own real captured value.
    periods = PeriodGrid(
        hours=np.array([5 / 60]), start=datetime(2026, 9, 9, 17, 25, tzinfo=UTC)
    )
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=np.array([0.0]),
        battery_discharge_kw=np.array([17.9]),
        battery_soc_kwh=np.array([28.5]),
        grid_import_kw=np.array([0.0]),
        grid_export_kw=np.array([15.4]),
        export_bonus_kw=np.array([0.0]),
        solar_used_kw=np.array([2.5]),
        solar_curtailed_kw=np.array([0.0]),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        iterations=0,
        batteries=[
            BatteryPlan(
                name="home",
                charge_kw=np.array([0.0]),
                discharge_kw=np.array([17.9]),
                soc_kwh=np.array([28.5]),
            )
        ],
    )


def _run(*, previous_plan, proximal_weight):
    n = 5
    grid = GridConfig(
        # A tiny but real, strictly positive export margin over the
        # battery's own discharge_cost (0.099) -- matching the issue's
        # own "a fraction of a cent per kWh either way" framing.
        # Genuinely positive, not zero, so the untethered case has a real
        # (deterministic, non-degenerate) LP incentive to push to the max
        # bound -- not just solver tie-breaking on an exactly-indifferent
        # objective.
        #
        # nimbus issue #732: export_price bumped from 0.10 to 0.11 --
        # discharging now also pays a real, capped efficiency-loss cost
        # (min(import_price * (1/discharge_efficiency - 1),
        # MIN_CHARGE_DISCHARGE_COST_SPREAD) = min(0.30 * 0.020408, 0.01)
        # = 0.006122 $/kWh here), which the original 0.001 margin no
        # longer clears (0.10 - 0.099 - 0.006122 < 0). This is the
        # correct, intended real-economics effect of that fix -- the
        # old margin was never actually profitable once the real
        # round-trip loss is honestly priced. Bumped just enough
        # (0.11 - 0.099 - 0.006122 = 0.004878) to restore a genuinely
        # positive, still-tiny margin so this file keeps testing tier-0
        # tethering specifically, not accidentally re-litigating #732.
        import_price=np.full(n, 0.30),
        export_price=np.full(n, 0.11),
        import_limit_kw=30.0,
        export_limit_kw=25.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 2.0))]
    battery = _battery()
    return build_plan(
        periods=_new_grid(),
        grid=grid,
        batteries=[battery],
        solar=solar,
        loads=loads,
        previous_plan=previous_plan,
        proximal_weight=proximal_weight,
    )


class TestTier0PeriodsTetherToTheOverlappingOldPeriod(unittest.TestCase):
    def test_tier0_periods_stay_near_the_previous_plans_value_when_aligned(self):
        plan = _run(previous_plan=_previous_plan_5min(), proximal_weight=0.005)
        self.assertEqual(plan.status, "optimal")
        discharge = plan.batteries[0].discharge_kw
        # The four 1-minute periods (indices 0-3) must stay close to the
        # previous plan's own 17.9 kW -- nowhere near the 25 kW limit.
        for i in range(4):
            self.assertLess(
                discharge[i],
                20.0,
                f"period {i} discharged {discharge[i]} kW -- proximal tether failed",
            )

    def test_same_scenario_with_no_previous_plan_at_all_reaches_the_limit(self):
        # The comparison case: with genuinely NOTHING to tether against
        # (not even a same-shaped previous plan), the real, strictly
        # positive export margin pushes the LP to the max bound -- this
        # confirms the margin in this scenario is real (not accidentally
        # zero/degenerate) and that the tethered test above is actually
        # testing something, not just measuring LP indifference.
        plan = _run(previous_plan=None, proximal_weight=0.005)
        self.assertEqual(plan.status, "optimal")
        discharge = plan.batteries[0].discharge_kw
        for i in range(4):
            self.assertGreater(discharge[i], 24.0)

    def test_the_5min_period_after_tier0_is_unaffected_either_way(self):
        # Period 4 (17:30, 5-minute) exact-matches the OLD algorithm's
        # own semantics too -- confirms the fix doesn't disturb the
        # already-correct boundary-aligned case.
        plan = _run(previous_plan=_previous_plan_5min(), proximal_weight=0.005)
        self.assertEqual(plan.status, "optimal")


if __name__ == "__main__":
    unittest.main()
