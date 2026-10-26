"""After a P2P block, the self-consume hours are pinned to no export, and
import is still allowed when the battery cannot carry the house (#1610)
(#1837).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _grid(fixed):
    n = len(fixed)
    return GridConfig(
        import_price=np.full(n, 0.23),
        export_price=np.full(n, 0.14),
        import_limit_kw=42.0,
        export_limit_kw=40.0,
        fixed_export_kw=np.asarray(fixed, dtype=float),
    )


def _plan(initial_soc):
    # 3 h block at 12 kW net, then 4 h post-window (pinned 0), house 1.5 kW.
    hours = np.array([1.0] * 7)
    fixed = [12.0, 12.0, 12.0, 0.0, 0.0, 0.0, 0.0]
    return build_plan(
        periods=PeriodGrid(
            hours=hours, start=datetime(2026, 10, 12, 11, 0, tzinfo=UTC)
        ),
        grid=_grid(fixed),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=60.0,
                initial_soc_kwh=initial_soc,
                min_soc_kwh=2.0,
                max_soc_kwh=60.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.96,
                discharge_efficiency=0.96,
                charge_cost=0.0,
                discharge_cost=0.01,
                salvage_value=0.05,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(7)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(7, 1.5))],
    )


def test_the_block_is_net_and_the_post_window_imports_instead_of_the_block():
    # Enough for the block (3 x 13.5 kWh / 0.96) but not the block AND 4 h of
    # house: the shortfall must land after midnight as ordinary import.
    plan = _plan(initial_soc=45.0)
    assert plan.status == "optimal"
    block, after = slice(0, 3), slice(3, 7)
    np.testing.assert_allclose(plan.grid_import_kw[block], 0.0, atol=1e-6)
    np.testing.assert_allclose(plan.grid_import_excess_kw, 0.0, atol=1e-6)
    np.testing.assert_allclose(plan.grid_export_kw[block], 12.0, atol=1e-6)
    assert float(np.sum(plan.grid_import_kw[after])) > 1.0
    np.testing.assert_allclose(plan.grid_export_kw[after], 0.0, atol=1e-6)
