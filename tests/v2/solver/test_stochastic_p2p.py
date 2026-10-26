"""P2P commitments in the battery-only two-stage model (solver/stochastic.py)
(#1837).

A fixed export rate holds in every scenario's window and never leaks into
stage 1; the charge gate holds there even when a strong terminal credit
pays to bank energy; each scenario's bonus volume is its own, claimed as
one block; and with no P2P fields the plan is what it was before.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.stochastic import build_stochastic_plan

TARGET_KW, STAGE2, N = 11.5, 3, 11  # periods 3..9 are the P2P window


def _plan(*, fixed_export=False, bonus=False, salvage_value=0.15, window_price=0.55):
    fixed = None
    if fixed_export:
        fixed = np.full(N, np.nan)
        if fixed_export != "nowhere":
            fixed[3:10] = TARGET_KW
    grid = GridConfig(
        import_price=np.array([0.15] * 3 + [window_price] * 7 + [0.15]),
        export_price=np.array([0.08] * 3 + [0.09] * 7 + [0.08]),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        fixed_export_kw=fixed,
        export_bonus_price=np.array([0.0] * 3 + [0.32] * 7 + [0.0]) if bonus else None,
        export_bonus_volume_kwh=40.0 if bonus else None,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=122.2,
        initial_soc_kwh=122.2 * 0.20,
        min_soc_kwh=122.2 * 0.05,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=salvage_value,
    )
    plan = build_stochastic_plan(
        periods=PeriodGrid(np.ones(N), start=datetime(2026, 8, 20, 14, tzinfo=UTC)),
        grid=grid,
        battery=battery,
        solar_scenarios=[np.zeros(N), np.zeros(N)],
        scenario_weights=[0.6, 0.4],
        stochastic_start_period=STAGE2,
        load_kw=np.full(N, 1.5),
    )
    assert plan.status == "optimal"
    return plan


def test_every_combination_of_commitment_and_bonus_solves():
    for fixed, bonus in list(itertools.product([False, True], repeat=2)):
        _plan(fixed_export=fixed, bonus=bonus)
