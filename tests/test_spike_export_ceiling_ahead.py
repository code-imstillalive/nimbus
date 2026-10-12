"""A P2P block pinned exactly could not sell more during a forecast price
spike, so the plan dumped its spare energy just before the block at a low
price. Reference household, 12 Oct 2026 plan, read 11 Oct 22:0x AEST:
16:30 sell 47c -> 30.6 kW discharge; 17:00-20:00 sell $10-21/kWh -> 11.5 kW,
the block's pin. While the spike override is armed, a block period whose
forecast import price is at or above the threshold now has its pin as a
floor, up to the export limit. Real HiGHS solves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
import solver_plan
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

# 16:30 (before the block), then 17:00-20:00 spike, then 20:00-24:00 block.
HOURS = np.array([0.5, 1.0, 1.0, 1.0, 4.0])
FIXED = [np.nan, 11.5, 11.5, 11.5, 11.5]
BUY = [0.60, 23.86, 24.02, 22.28, 0.43]
SELL = [0.47, 21.62, 21.77, 20.19, 0.32]
ARMED = {"solver_price_spike_override_armed": True, "solver_price_spike_threshold": 1.0}


def _plan(ceiling):
    return build_plan(
        periods=PeriodGrid(
            hours=HOURS, start=datetime(2026, 10, 12, 6, 30, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.array(BUY),
            export_price=np.array(SELL),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=np.array(FIXED, dtype=float),
            fixed_export_max_kw=None
            if ceiling is None
            else np.array(ceiling, dtype=float),
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=120.0,
                initial_soc_kwh=118.0,
                min_soc_kwh=2.0,
                max_soc_kwh=120.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.97,
                discharge_efficiency=0.97,
                charge_cost=0.0,
                discharge_cost=0.01,
                salvage_value=0.05,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(len(HOURS))),
        loads=[LoadConfig(name="house", forecast_kw=np.full(len(HOURS), 1.3))],
    )


def test_the_ceiling_is_set_only_on_armed_spike_block_periods():
    ceiling = solver_plan.spike_export_ceiling(ARMED, FIXED, BUY, [40.0] * 5)
    assert ceiling is not None
    assert np.isnan(ceiling[0])  # not a block period
    assert ceiling[1:4] == [40.0, 40.0, 40.0]  # spike, in the block
    assert np.isnan(ceiling[4])  # block, no spike: still exactly 11.5


def test_off_unless_armed_with_a_threshold():
    assert solver_plan.spike_export_ceiling({}, FIXED, BUY, [40.0] * 5) is None
    unarmed = {**ARMED, "solver_price_spike_override_armed": False}
    assert solver_plan.spike_export_ceiling(unarmed, FIXED, BUY, [40.0] * 5) is None
    no_threshold = {**ARMED, "solver_price_spike_threshold": 0.0}
    assert (
        solver_plan.spike_export_ceiling(no_threshold, FIXED, BUY, [40.0] * 5) is None
    )
    assert solver_plan.spike_export_ceiling(ARMED, None, BUY, [40.0] * 5) is None


def test_without_it_the_plan_dumps_before_the_block():
    plan = _plan(None)
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_export_kw[1:4], 11.5, atol=1e-6)
    assert plan.battery_discharge_kw[0] > 20.0  # the 16:30 dump


def test_with_it_the_energy_is_kept_for_the_spike_and_the_block_still_delivered():
    ceiling = solver_plan.spike_export_ceiling(ARMED, FIXED, BUY, [40.0] * 5)
    plan = _plan(ceiling)
    assert plan.status == "optimal"
    assert plan.battery_discharge_kw[0] < 1.0  # no dump at 47c
    assert float(np.min(plan.grid_export_kw[1:4])) >= 11.5 - 1e-6  # floor kept
    assert float(np.max(plan.grid_export_kw[1:4])) > 30.0  # sells into the spike
    np.testing.assert_allclose(plan.grid_export_kw[4], 11.5, atol=1e-6)  # block after
    np.testing.assert_allclose(plan.grid_import_excess_kw, 0.0, atol=1e-6)
    # And it is worth it.
    assert plan.total_cost < _plan(None).total_cost - 100.0
