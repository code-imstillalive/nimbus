"""The install's own Discharge Cost is what the solve uses in the daytime too
(13 Oct 2026).

Reference household, 12:46 AEST: the current 5-minute interval's sell price
jumped to 43.5c (buy 49.1c) and the plan sold nothing, with Discharge Cost set
to 0.01. The solve was costing daytime discharge at 0.09 -- HAEO's retired
5pm/midnight/7am cost schedule, the hidden default of
`scheduled_discharge_cost_rate()` from 07:00 to 17:00. Replaying production's
own solve read-only with the spike injected sold 0 kW at 0.09 and the full
40 kW at 0.01. These tests pin both halves: the rate the writer builds, and
what the LP does with it at those prices. Real HiGHS solves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
import pytest
import solver_writer
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

CAP = 119.756
EFF = 0.9262828941527529  # the reference household's 85.8% round trip


def test_the_writer_costs_daytime_discharge_at_the_setting():
    cfg = {"solver_discharge_cost": 0.01}
    rates = [solver_writer.scheduled_discharge_cost_rate(cfg, h) for h in range(24)]
    assert rates == [0.01] * 24


def _plan(discharge_cost: float):
    # 12:46 on a 5-minute grid: one spiked period, then the afternoon at the
    # 13:00 forecast (sell 29.3c, buy 33.5c) with room to refill.
    n = 24
    sell = np.full(n, 0.2927)
    buy = np.full(n, 0.335)
    sell[0], buy[0] = 0.4348, 0.4913
    return build_plan(
        periods=PeriodGrid(
            hours=np.full(n, 1 / 12), start=datetime(2026, 10, 13, 2, 45, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=buy,
            export_price=sell,
            import_limit_kw=42.0,
            export_limit_kw=40.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=CAP,
                initial_soc_kwh=CAP * 0.45,
                min_soc_kwh=CAP * 0.02,
                max_soc_kwh=CAP,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=EFF,
                discharge_efficiency=EFF,
                charge_cost=0.005,
                discharge_cost=discharge_cost,
                # energy left at the end is worth refilling for: the plan
                # must buy back what it sells
                salvage_value=0.40,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
    )


def test_at_the_setting_it_sells_into_the_spike():
    plan = _plan(0.01)
    assert plan.status == "optimal"
    # the full 40 kW out of the battery: 1 kW to the house, 39 kW sold
    assert plan.battery_discharge_kw[0] == pytest.approx(40.0, abs=1e-6)
    assert plan.grid_export_kw[0] == pytest.approx(39.0, abs=1e-6)


def test_at_the_old_hidden_daytime_cost_it_did_not():
    plan = _plan(0.09)
    assert plan.status == "optimal"
    assert plan.battery_discharge_kw[0] == pytest.approx(0.0, abs=1e-6)
    assert plan.grid_export_kw[0] == pytest.approx(0.0, abs=1e-6)
