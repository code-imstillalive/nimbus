"""An armed price-spike override with the battery near its minimum SoC.

Found by tests/v2's spike property (#1837, 18 Oct 2026). The override
(#567) hard-pinned period 0's discharge to the set rate. The same-period
wash-trade guard caps period 0's draw at the energy above min_soc_kwh. When
the battery was within one period's draw of its minimum, the two rows
contradicted each other and the whole plan came back infeasible: no plan at
all, at exactly the moment a spike was being sold into.
"""

from __future__ import annotations

import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

H = 5 / 60


def _plan(initial: float, spike: float | None):
    z = np.zeros(2)
    return build_plan(
        periods=PeriodGrid(np.full(2, H)),
        grid=GridConfig(
            import_price=z, export_price=z, import_limit_kw=0.0, export_limit_kw=1.0
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=1.0,
                initial_soc_kwh=initial,
                min_soc_kwh=0.25,
                max_soc_kwh=1.0,
                max_charge_kw=1.0,
                max_discharge_kw=1.0,
                charge_efficiency=0.875,
                discharge_efficiency=0.875,
                charge_cost=0.005,
                discharge_cost=0.0625,
                salvage_value=0.0,
                spike_override_discharge_kw=spike,
            )
        ],
        solar=SolarConfig(forecast_kw=z),
        loads=[LoadConfig(name="h", forecast_kw=z)],
    )


def test_near_the_floor_it_discharges_what_it_can():
    """0.27 kWh against a 0.25 minimum: 0.02 kWh above it, x 0.875 / (5 min)
    = 0.21 kW is all the guard allows."""
    plan = _plan(0.27, 0.5)
    assert plan.status == "optimal"
    assert abs(plan.battery_discharge_kw[0] - 0.02 * 0.875 / H) < 1e-6


def test_at_the_floor_it_discharges_nothing_and_still_plans():
    plan = _plan(0.25, 0.5)
    assert plan.status == "optimal"
    assert plan.battery_discharge_kw[0] <= 1e-9


def test_with_room_it_discharges_the_set_rate():
    plan = _plan(0.5, 0.5)
    assert plan.status == "optimal"
    assert abs(plan.battery_discharge_kw[0] - 0.5) < 1e-9
