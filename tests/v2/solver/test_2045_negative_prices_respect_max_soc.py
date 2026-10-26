"""#2045 (Mark Purcell, from #2043's review): with every price negative a
battery filled past its max_soc_kwh, because the derived soft-SoC penalty
was 10x the largest SIGNED price. Import -1 and export -2 gave a signed
maximum of -1, the 0.01 floor won, and a 0.10 $/kWh penalty lost to being
paid 1 $/kWh to import. The penalty is now sized on price magnitude.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import (
    DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER,
    build_plan,
    soft_soc_penalty,
)

MAX_SOC = 4.375


def _battery(name: str = "a", **overrides) -> BatteryConfig:
    cfg = {
        "name": name,
        "capacity_kwh": 5.0,
        "initial_soc_kwh": 0.0,
        "min_soc_kwh": 0.0,
        "max_soc_kwh": MAX_SOC,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.005,
        "discharge_cost": 0.005,
        "salvage_value": 0.0,
    }
    cfg.update(overrides)
    return BatteryConfig(**cfg)


def _plan(imports, exports, batteries):
    n = len(imports)
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.array(imports, dtype=float),
            export_price=np.array(exports, dtype=float),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        ),
        batteries=batteries,
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
    )


def test_the_penalty_is_sized_on_magnitude():
    assert soft_soc_penalty([-1.0], [-2.0]) == pytest.approx(
        2.0 * DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER
    )
    # Ordinary positive prices: unchanged from the signed maximum.
    assert soft_soc_penalty([0.30, 0.10], [0.05]) == pytest.approx(
        0.30 * DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER
    )
    # Other rates (salvage, terminal value) by magnitude too, and the floor.
    assert soft_soc_penalty([0.1], [0.1], [-0.5]) == pytest.approx(
        0.5 * DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER
    )
    assert soft_soc_penalty([0.0], [0.0]) == pytest.approx(
        0.01 * DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER
    )


def test_no_price_at_all_is_the_floor():
    """Empty price arrays and no other rate: the floor, not a TypeError."""
    assert soft_soc_penalty([], []) == pytest.approx(
        0.01 * DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER
    )


_price = st.floats(-2.0, 2.0, allow_nan=False).map(lambda v: round(v, 3))


@settings(max_examples=60, deadline=None, derandomize=True)
@given(st.lists(st.tuples(_price, _price), min_size=2, max_size=6))
def test_no_price_pays_enough_to_overfill(prices):
    """Starting inside the range, no mix of signed prices makes the plan
    go above max_soc_kwh."""
    imports = [i for i, _ in prices]
    exports = [min(e, i) for i, e in prices]  # export never above import
    plan = _plan(imports, exports, [_battery(initial_soc_kwh=2.0)])
    assert plan.status == "optimal"
    assert max(plan.batteries[0].soc_kwh) <= MAX_SOC + 1e-6
