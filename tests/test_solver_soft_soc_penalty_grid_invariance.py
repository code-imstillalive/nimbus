"""nimbus issue #338: the soft-SoC underfill/overfill penalty used to be
scaled by hours[t] while the terminal-value segment credit it has to
dominate is a bare $/kWh. On a 1 h grid the 10x safety margin held; on
the production 5-minute grid it collapsed to 0.83x, and the LP could
inflate underfill[n-1] to its upper bound, bank phantom terminal credit
for energy that does not exist, and -- worse -- sell real stored energy
it should have held (the exact "keeps discharging toward the floor"
failure terminal_value_period_indices was built to prevent).

These tests pin the fix: the same physical scenario must produce the
same dispatch and the same total_cost whatever the period length.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
import pytest
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

HORIZON_H = 12.0
GRIDS_H = (1.0, 0.25, 1.0 / 12.0)  # hourly, 15-min, and the production 5-min tier


def _plan(hours_per_period: float, *, initial_soc: float, export_price: float):
    n = round(HORIZON_H / hours_per_period)
    periods = PeriodGrid(
        hours=np.full(n, hours_per_period), start=datetime(2026, 9, 2, tzinfo=UTC)
    )
    grid = GridConfig(
        import_price=np.full(n, 0.30),
        export_price=np.full(n, export_price),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        capacity_kwh=100.0,
        initial_soc_kwh=initial_soc,
        min_soc_kwh=20.0,
        max_soc_kwh=90.0,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.005,
        discharge_cost=0.005,
        salvage_value=0.0,
        # One breakpoint whose rate is the dominant price signal -- the
        # configuration where the old hours-scaled penalty lost dominance.
        terminal_value_breakpoints=[(70.0, 0.30)],
        terminal_value_period_indices=[n - 1],
    )
    plan = build_plan(
        periods=periods,
        grid=grid,
        battery=battery,
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
    )
    assert plan.status == "optimal"
    exported_kwh = float(np.sum(plan.grid_export_kw * periods.hours))
    return plan, exported_kwh


@pytest.mark.parametrize("hours_per_period", GRIDS_H)
def test_holds_energy_below_terminal_rate_on_every_grid(hours_per_period):
    """Export at 0.285 is worth less than the 0.30 terminal rate, so the
    right answer is to hold every kWh -- on the 5-min grid the old code
    instead dumped ~20 kWh down to the breakpoint."""
    plan, exported = _plan(hours_per_period, initial_soc=90.0, export_price=0.285)
    assert exported == pytest.approx(0.0, abs=1e-6)
    assert plan.battery_soc_kwh[-1] == pytest.approx(90.0, abs=1e-6)


@pytest.mark.parametrize("hours_per_period", GRIDS_H)
def test_terminal_credit_is_never_phantom(hours_per_period):
    """Flat 0.30 everywhere, start at 50 kWh, nothing to do: the only
    cost term is the terminal credit on the 30 kWh above the floor,
    (50 - 20) * 0.30 = $9.00. The old 5-min-grid answer was $10.00 --
    a full extra 20 kWh (= underfill's ub) of credit conjured from
    nowhere."""
    plan, _exported = _plan(hours_per_period, initial_soc=50.0, export_price=0.30)
    assert plan.total_cost == pytest.approx(-9.0, abs=1e-3)
    assert plan.battery_soc_kwh[-1] == pytest.approx(50.0, abs=1e-6)


def test_total_cost_is_grid_invariant():
    costs = [
        _plan(h, initial_soc=90.0, export_price=0.285)[0].total_cost for h in GRIDS_H
    ]
    assert max(costs) - min(costs) < 1e-3
