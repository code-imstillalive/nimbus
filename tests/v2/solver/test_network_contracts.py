"""Layer 1 of the rebuilt suite (#1837): two `network.py` contracts the 21 Oct
classification of the surviving mutants found neither test set checking
(seed 1837; the other three survivors are equivalent mutants, recorded on
#1837).

- The stochastic plan solves "the same inputs `network.build_plan(**
  build_kwargs)` would solve deterministically" (stochastic_dispatch.py),
  but calls `_build_plan_once` directly: an argument the caller leaves out
  must mean the same in both, so every default they share is equal.
- An adequacy load's reported profit is what its energy was worth less what
  it cost at the plan's own marginal price (#482), in kWh, so a period's
  length counts.
"""

from __future__ import annotations

import inspect

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver import network
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan
from test_plan_invariants import SETTINGS, _num


def test_build_plan_and_the_stochastic_entry_share_every_default():
    once = inspect.signature(network._build_plan_once).parameters
    public = inspect.signature(network.build_plan).parameters
    shared = [
        name
        for name, p in public.items()
        if name in once and p.default is not inspect.Parameter.empty
    ]
    assert "risk_aversion" in shared  # the check is not vacuous
    differ = {
        name: (public[name].default, once[name].default)
        for name in shared
        if public[name].default != once[name].default
    }
    assert differ == {}


@SETTINGS
@given(
    st.integers(min_value=2, max_value=8),
    st.sampled_from([0.25, 0.5]),
    _num(1.0, 4.0),
    _num(0.05, 0.4),
    _num(0.5, 1.0),
)
def test_an_adequacy_loads_profit_is_its_value_less_its_marginal_cost(
    n, width, power, price, value
):
    """profit_horizon = value x energy - marginal_cost, both in kWh."""
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
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
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.0,
                available=False,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=power,
                target_kwh=power * width,  # one period's worth
                deadline_period=n - 1,
                shortfall_price=20.0,
                value_per_kwh=value,
            )
        ],
    )
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    energy = float(np.sum(al.power_kw * width))
    assert energy > 0.0  # worth more than it costs: it runs
    expected = value * energy - al.marginal_cost
    assert al.profit_horizon is not None
    assert abs(al.profit_horizon - expected) <= 1e-6 * max(1.0, abs(expected))
