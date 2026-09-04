"""Regression test for nimbus issue #354 Defect 1 (Mark Purcell): the
per-real-calendar-day P2P export-bonus cap in solver/stochastic.py was
applied independently once for stage 1's own period range and again,
separately, for each scenario's own stage-2 period range -- so a real
calendar day that both stage 1 and a scenario's stage 2 fall on (the
day containing the stochastic_start_period branch point) got capped
TWICE at the full real daily volume, letting a single scenario-world
plan as if 2x the real committed daily volume were available on that
one day.

This test constructs a scenario where stage 1 (periods 0-3) and stage 2
(periods 4-7) both fall on the SAME real calendar day, both with a real
economic incentive (a nonzero export_bonus_price) to claim bonus-
eligible export volume, and plenty of real physical battery/grid
headroom to claim far more than the configured daily cap if nothing
stopped it. Before the fix: stage 1 alone claims up to the full cap,
and each scenario's own stage 2 ALSO independently claims up to the
full cap, for a combined total up to 2x the real cap on that one shared
day. After the fix: the combined stage-1 + stage-2 total for each
scenario is bound to the real configured cap.

Real, direct call into the actual build_stochastic_plan() -- not a
reimplementation -- same construction pattern as this project's sibling
test_solver_stochastic_p2p.py.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.stochastic import build_stochastic_plan

_STOCHASTIC_START = 4  # periods 0-3 stage 1, periods 4-7 stage 2
_N = 8
_CAP_KWH = 10.0


def _same_day_branch_scenario():
    """All 8 periods (stage 1: 0-3, stage 2: 4-7) fall on the SAME real
    calendar day -- the exact condition nimbus issue #354 describes."""
    hours = np.array([1.0] * _N)
    periods = PeriodGrid(hours=hours, start=datetime(2026, 8, 20, 0, 0, tzinfo=UTC))

    import_price = np.full(_N, 0.20)
    export_price = np.full(_N, 0.05)
    # Nonzero for EVERY period, both stage 1 and stage 2 -- a real
    # economic incentive to claim bonus-eligible export on both sides of
    # the branch point, on the same real calendar day.
    export_bonus_price = np.full(_N, 0.30)

    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        export_bonus_price=export_bonus_price,
        export_bonus_volume_kwh=_CAP_KWH,
    )
    battery = BatteryConfig(
        capacity_kwh=100.0,
        initial_soc_kwh=90.0,
        min_soc_kwh=5.0,
        max_soc_kwh=100.0,
        max_charge_kw=20.0,
        max_discharge_kw=20.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
    n_scenarios = 2
    solar_scenarios = [np.zeros(_N) for _ in range(n_scenarios)]
    weights = [0.5, 0.5]
    load_kw = np.zeros(_N)
    return periods, grid, battery, solar_scenarios, weights, load_kw


class TestP2PCapNotDoubledOnBranchDay(unittest.TestCase):
    def test_combined_stage1_plus_stage2_volume_never_exceeds_the_real_cap(self):
        periods, grid, battery, solar_scenarios, weights, load_kw = (
            _same_day_branch_scenario()
        )
        plan = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan.status, "optimal")

        stage1_bonus_kwh = float(
            np.sum(plan.stage1_export_bonus_kw * periods.hours[:_STOCHASTIC_START])
        )
        for s in range(len(solar_scenarios)):
            stage2_bonus_kwh = float(
                np.sum(
                    plan.stage2_export_bonus_kw[s] * periods.hours[_STOCHASTIC_START:]
                )
            )
            combined = stage1_bonus_kwh + stage2_bonus_kwh
            self.assertLessEqual(
                combined,
                _CAP_KWH + 0.01,
                f"scenario {s}: stage1 ({stage1_bonus_kwh:.2f}kWh) + "
                f"stage2 ({stage2_bonus_kwh:.2f}kWh) = {combined:.2f}kWh "
                f"combined on the one real shared calendar day -- exceeds "
                f"the configured {_CAP_KWH}kWh daily cap (the exact "
                f"double-counting nimbus issue #354 describes)",
            )

    def test_the_real_cap_is_still_meaningfully_used_not_starved_to_zero(self):
        """Sanity check the test scenario itself is well-formed: a real
        economic incentive to claim SOME bonus volume must exist, or the
        assertion above would trivially pass with everything at 0kWh."""
        periods, grid, battery, solar_scenarios, weights, load_kw = (
            _same_day_branch_scenario()
        )
        plan = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan.status, "optimal")
        stage1_bonus_kwh = float(
            np.sum(plan.stage1_export_bonus_kw * periods.hours[:_STOCHASTIC_START])
        )
        stage2_bonus_kwh = float(
            np.sum(plan.stage2_export_bonus_kw[0] * periods.hours[_STOCHASTIC_START:])
        )
        self.assertGreater(
            stage1_bonus_kwh + stage2_bonus_kwh,
            _CAP_KWH * 0.5,
            "the combined claimed bonus volume is suspiciously low -- "
            "this test scenario may not actually incentivize claiming "
            "the cap at all, making the main assertion vacuous",
        )


if __name__ == "__main__":
    unittest.main()
