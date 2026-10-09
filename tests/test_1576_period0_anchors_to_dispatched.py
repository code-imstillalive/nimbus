"""nimbus #1576: period 0's proximal anchor targets the previously DISPATCHED
value (the old plan's period 0), not the time-aligned old period.

A solve lands ~15 s after the previous one. Once a minute boundary has
passed, the new period 0 starts inside the old plan's period 1, which in the
tiered grid is often a longer period with a different value. #635's
containment rule then anchored period 0 to that, while the automation reads
period 0 as the live command. Measured on devhub, 2 Oct 2026 (#1417): the
anchor targeted old[1] in 11 of 18 crossings, a median 3.19 kW from what was
dispatched, and holding the dispatched value was free at the worst one.

Economics here are near-indifferent (the #635 test's tiny export margin), so
the anchor decides where period 0 lands.
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
from solver.network import BatteryPlan, Plan, _anchor_source, build_plan

DISPATCHED_KW = 8.0  # old period 0 -- what the inverter was told
PLANNED_NEXT_KW = 17.9  # old period 1 -- the old plan's next 5 minutes


def _battery() -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=4.0,
        max_soc_kwh=40.0,
        max_charge_kw=25.0,
        max_discharge_kw=25.0,
        charge_efficiency=0.98,
        discharge_efficiency=0.98,
        charge_cost=0.01,
        discharge_cost=0.099,
        salvage_value=0.0,
    )


def _previous_plan() -> Plan:
    # Tiered like a real plan: a 1-minute period 0 (10:00-10:01), then a
    # 5-minute period 1 (10:01-10:06) planning a different rate.
    periods = PeriodGrid(
        hours=np.array([1 / 60, 5 / 60]), start=datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    )
    discharge = np.array([DISPATCHED_KW, PLANNED_NEXT_KW])
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=np.zeros(2),
        battery_discharge_kw=discharge,
        battery_soc_kwh=np.array([29.8, 28.3]),
        grid_import_kw=np.zeros(2),
        grid_export_kw=discharge - 2.0,
        export_bonus_kw=np.zeros(2),
        solar_used_kw=np.zeros(2),
        solar_curtailed_kw=np.zeros(2),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        soc_penalty_cost=0.0,
        iterations=0,
        batteries=[
            BatteryPlan(
                name="home",
                charge_kw=np.zeros(2),
                discharge_kw=discharge,
                soc_kwh=np.array([29.8, 28.3]),
            )
        ],
    )


def _solve(start: datetime):
    n = 5
    hours = np.array([1 / 60, 1 / 60, 1 / 60, 1 / 60, 5 / 60])
    return build_plan(
        periods=PeriodGrid(hours=hours, start=start),
        grid=GridConfig(
            # The #635 test's tiny but real export margin: near-indifferent.
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.11),
            import_limit_kw=30.0,
            export_limit_kw=25.0,
        ),
        batteries=[_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        previous_plan=_previous_plan(),
        proximal_weight=0.005,
    )


class TestAnchorSource(unittest.TestCase):
    def test_period_0_inside_old_period_1_anchors_to_old_period_0(self):
        self.assertEqual(_anchor_source(0, 1), 0)
        self.assertEqual(_anchor_source(0, 0), 0)

    def test_every_later_period_keeps_containment(self):
        self.assertEqual(_anchor_source(1, 1), 1)
        self.assertEqual(_anchor_source(3, 2), 2)

    def test_a_stale_previous_plan_keeps_containment_for_period_0(self):
        """Period 0 inside old period 2 or later: the old plan's period 0
        is long past, not what the hardware is doing now."""
        self.assertEqual(_anchor_source(0, 2), 2)


class TestPeriod0HoldsTheDispatchedValue(unittest.TestCase):
    def test_a_solve_just_past_the_minute_holds_what_was_dispatched(self):
        # 10:01 is inside old period 1 (10:01-10:06): containment would have
        # anchored period 0 to 17.9 kW. It is the 8.0 kW the inverter holds.
        plan = _solve(datetime(2026, 10, 2, 10, 1, tzinfo=UTC))
        self.assertEqual(plan.status, "optimal")
        p0 = float(plan.batteries[0].discharge_kw[0])
        self.assertAlmostEqual(p0, DISPATCHED_KW, places=3)
        self.assertGreater(abs(p0 - PLANNED_NEXT_KW), 5.0)

    def test_later_periods_still_follow_the_old_plans_timeline(self):
        plan = _solve(datetime(2026, 10, 2, 10, 1, tzinfo=UTC))
        # Periods 1-3 (10:02-10:04) lie inside old period 1 and anchor there.
        for i in (1, 2, 3):
            self.assertAlmostEqual(
                float(plan.batteries[0].discharge_kw[i]), PLANNED_NEXT_KW, places=3
            )


if __name__ == "__main__":
    unittest.main()
