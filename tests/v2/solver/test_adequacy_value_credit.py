"""Layer 1 of the rebuilt suite (#1837): an adequacy load's value credit
(`value_per_kwh`, #482) applies whether or not the load has windows.

Found by #1837's cutover check, 21 Oct 2026: #612's change (#634, v0.94.206)
put a windowed-only `continue` above the credit, so a load with no windows
lost it. With a cheap shortfall price it then never ran even where its value
was far above the price, and with a dear one it ran only for the shortfall.
The credit is capped at the load's target (#606), so a load worth more than
its energy costs runs exactly its target, windows or not.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver.elements import (
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan
from test_plan_invariants import SETTINGS, _num


def _plan(value, price, power, windowed, shortfall):
    n = 24
    target = power * 2.0  # two hours of the load
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.zeros(n),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=10.0,
                initial_soc_kwh=5.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.0,
                max_charge_kw=3.0,
                max_discharge_kw=3.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.1,
                available=False,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 0.5))],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=2,
                deadline_period=8,
                shortfall_price=shortfall,
                value_per_kwh=value,
                windows=(
                    (
                        AdequacyWindow(
                            earliest_period=2, deadline_period=8, target_kwh=target
                        ),
                    )
                    if windowed
                    else None
                ),
            )
        ],
    )


@SETTINGS
@given(_num(0.02, 0.9), _num(0.02, 0.6), _num(0.3, 2.0), st.booleans())
def test_the_value_credit_applies_with_or_without_windows(
    value, price, power, windowed
):
    """With shedding nearly free (1c/kWh), the load runs its target exactly
    when its value beats the price, and nothing otherwise; a single window
    covering the same span plans the same."""
    if abs(value - price) < 0.02:
        return
    plan = _plan(value, price, power, windowed, shortfall=0.01)
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    energy = float(np.sum(al.power_kw))
    if value > price:
        assert abs(energy - power * 2.0) < 1e-4
        assert abs(al.profit_horizon - (value - price) * energy) < 1e-6
    else:
        assert energy < 1e-6
        assert abs(al.profit_horizon) < 1e-6
    other = _plan(value, price, power, not windowed, shortfall=0.01)
    np.testing.assert_allclose(
        other.adequacy_loads[0].power_kw.sum(), energy, atol=1e-4
    )
