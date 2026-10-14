"""nimbus #1761: a P2P block the battery cannot fund made the whole solve
infeasible, and the #773 fallback published 0 kW in every period.

Root cause (14 Oct 2026, reference household's inputs at 15:42 AEST with SoC
28.7 kWh and block 1 at 17:00-24:00 x 11.5 kW): inside a pinned period
`grid_export` is fixed at the block rate, and the wash-trade rule
(`grid_export <= solar_used + discharge`) forbids grid import from funding
export, so the battery alone has to deliver the whole block. Charging is gated
to 0 inside the block, so with too little energy before it there is no
feasible plan at all. `grid_import_excess` (#390) only covers the house, never
export. Dropping either the wash-trade row family, the SoC recursion, or the
export pin made the model feasible; nothing else did.

The block is now an exact ceiling with a priced floor: each kW short costs
more than importing at the #390 excess penalty plus anything the energy could
earn elsewhere, so a block the battery CAN fund is still delivered exactly.
Real HiGHS solves.
"""

from __future__ import annotations

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
from solver.network import build_plan

N = 8  # 16:00-24:00 AEST, hourly
BLOCK = slice(1, 8)  # 17:00-24:00
RATE = 11.5


def _plan(initial_kwh: float):
    fixed = np.full(N, np.nan)
    fixed[BLOCK] = RATE
    return build_plan(
        periods=PeriodGrid(
            hours=np.full(N, 1.0), start=datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(N, 0.20),
            export_price=np.full(N, 0.12),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=120.0,
                initial_soc_kwh=initial_kwh,
                min_soc_kwh=2.4,
                max_soc_kwh=120.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.93,
                discharge_efficiency=0.93,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(N, 1.0))],
    )


def test_a_block_the_battery_cannot_fund_still_solves():
    # 10 kWh + one hour at 40 kW before 17:00 is ~47 kWh; 7 h x 11.5 kW needs
    # ~87 kWh at the battery.
    plan = _plan(10.0)
    assert plan.status == "optimal"


def test_it_charges_flat_out_before_the_block_and_sells_what_it_can():
    plan = _plan(10.0)
    assert abs(plan.battery_charge_kw[0] - 40.0) < 1e-6
    exported = float(np.sum(plan.grid_export_kw[BLOCK]))
    assert 30.0 < exported < RATE * 7
    assert np.all(plan.grid_export_kw[BLOCK] <= RATE + 1e-6)


def test_a_block_the_battery_can_fund_is_still_exact():
    plan = _plan(110.0)
    assert plan.status == "optimal"
    assert np.allclose(plan.grid_export_kw[BLOCK], RATE, atol=1e-6)
