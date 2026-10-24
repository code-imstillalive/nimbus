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
import pytest
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


def test_a_fixed_export_rate_holds_in_every_scenarios_window_and_not_before():
    plan = _plan(fixed_export=True)
    assert not np.allclose(plan.stage1_grid_export_kw, TARGET_KW, atol=0.01)
    for exports in plan.stage2_grid_export_kw:
        np.testing.assert_allclose(exports[:7], TARGET_KW, atol=1e-4)
        assert exports[7] != pytest.approx(TARGET_KW, abs=0.01)  # after the window


def test_no_scenario_charges_inside_the_window_even_when_banking_pays():
    """A terminal credit well above the window price would pay to charge
    and bank; the commitment's gate holds anyway."""
    plan = _plan(fixed_export=True, salvage_value=0.9, window_price=0.2)
    for charge, exports in zip(
        plan.stage2_charge_kw, plan.stage2_grid_export_kw, strict=True
    ):
        np.testing.assert_allclose(charge[:7], 0.0, atol=1e-4)
        np.testing.assert_allclose(exports[:7], TARGET_KW, atol=1e-4)
    assert float(np.sum(plan.stage1_charge_kw)) > 0  # it banks before the window


def test_each_scenario_claims_its_own_bonus_volume_as_one_block():
    plan = _plan(bonus=True)
    for bonus in plan.stage2_export_bonus_kw:
        assert float(np.sum(bonus[:7])) == pytest.approx(40.0, abs=1e-3)
        on = [float(x) > 0.01 for x in bonus[:7]]
        assert sum(a != b for a, b in itertools.pairwise(on)) <= 2


def test_without_p2p_fields_the_plan_is_unchanged_and_claims_nothing():
    # A commitment array that commits nowhere is the same as none at all.
    a, b = _plan(), _plan(fixed_export="nowhere")
    np.testing.assert_allclose(a.stage1_charge_kw, b.stage1_charge_kw)
    for x, y in zip(a.stage2_grid_export_kw, b.stage2_grid_export_kw, strict=True):
        np.testing.assert_allclose(x, y)
    assert float(np.sum(a.stage1_export_bonus_kw)) == 0.0
    assert all(float(np.sum(s)) == 0.0 for s in a.stage2_export_bonus_kw)


@pytest.mark.parametrize(
    ("fixed", "bonus"), list(itertools.product([False, True], repeat=2))
)
def test_every_combination_of_commitment_and_bonus_solves(fixed, bonus):
    _plan(fixed_export=fixed, bonus=bonus)


def test_every_scenario_starts_from_where_stage_one_left_the_battery():
    """SoC follows the flows across the stage boundary, in every scenario."""
    plan = _plan(fixed_export=True)
    eff_c, eff_d = 0.975, 0.975
    last = float(plan.stage1_soc_kwh[-1])
    for charge, discharge, soc in zip(
        plan.stage2_charge_kw,
        plan.stage2_discharge_kw,
        plan.stage2_soc_kwh,
        strict=True,
    ):
        expected = last + float(charge[0]) * eff_c - float(discharge[0]) / eff_d
        assert float(soc[0]) == pytest.approx(expected, abs=1e-6)
