"""The default soft SoC penalty must dominate the battery's own salvage.

Found by tests/v2's bounds property (#1837, 18 Oct 2026): at zero prices with
a $0.25/kWh salvage, the default penalty (10 x max(import, export,
terminal-value breakpoints, 0.01) = $0.10) was below the salvage, so the plan
charged past Max SoC to earn it: 0.80 kWh against 0.75 (#328's soft bound).
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

N = 12


def _plan(salvage: float, initial: float = 0.5):
    z = np.zeros(N)
    load = z.copy()
    load[-1] = 1.0
    return build_plan(
        periods=PeriodGrid(np.full(N, 5 / 60)),
        grid=GridConfig(
            import_price=z, export_price=z, import_limit_kw=1.0, export_limit_kw=0.0
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=1.0,
                initial_soc_kwh=initial,
                min_soc_kwh=0.25,
                max_soc_kwh=0.75,
                max_charge_kw=1.0,
                max_discharge_kw=1.0,
                charge_efficiency=0.875,
                discharge_efficiency=0.875,
                charge_cost=0.005,
                discharge_cost=0.0625,
                salvage_value=salvage,
            )
        ],
        solar=SolarConfig(forecast_kw=z),
        loads=[LoadConfig(name="h", forecast_kw=load)],
    )


def test_a_salvage_above_every_price_does_not_overfill_max_soc():
    plan = _plan(0.25)
    assert plan.status == "optimal"
    assert plan.battery_soc_kwh.max() <= 0.75 + 1e-6
    assert plan.soc_penalty_cost <= 1e-9
