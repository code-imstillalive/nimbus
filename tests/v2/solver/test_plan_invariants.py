"""Layer 1 of the rebuilt suite (#1837): properties every solved plan holds.

Hypothesis generates sites (one battery, solar, a load, prices, limits) and
each plan from `build_plan` is checked against physics and the LP's own
promises, not against an example's expected numbers:

- energy balance in every period;
- solar used plus curtailed equals the forecast;
- SoC follows the charge and discharge flows through the efficiencies;
- every flow within its limit, SoC within its bounds;
- no charge with discharge, and no import with export, in one period
  (#238, #1535);
- a battery never makes the plan dearer than leaving it idle;
- an adequacy load (hot water, a deferrable) stays within its power and
  window, its delivered and shortfall energy add up to its target, and it
  meets a target it can reach when a shortfall is dear.

Each property states the issue numbers whose single-case pins it subsumes,
so `git grep '#NNN'` still finds them.
"""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan

TOL = 1e-5
# derandomize: the same generated sites on every run, so a mutation
# comparison (scripts/mutation_compare.py) measures the code, not the draw.
SETTINGS = settings(
    max_examples=40,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _num(lo: float, hi: float):
    """A float in [lo, hi] at 4 decimals: real settings, not denormals.
    (Unrounded, Hypothesis found an export limit of 1.2e-38 kW, which HiGHS
    refuses as a bound, so build_plan raises instead of returning an error
    plan: recorded on #1837, not a realistic input.)"""
    return st.floats(min_value=lo, max_value=hi).map(lambda x: round(x, 4))


@st.composite
def sites(draw):
    n = draw(st.integers(min_value=2, max_value=24))
    width = draw(st.sampled_from([5 / 60, 0.25, 0.5, 1.0]))
    price = _num(-0.2, 2.0)
    import_price = np.array(draw(st.lists(price, min_size=n, max_size=n)))
    # Export never above import in this generator: the spread case is its
    # own property below.
    discount = np.array(draw(st.lists(_num(0.0, 0.3), min_size=n, max_size=n)))
    export_price = import_price - discount
    load = np.array(draw(st.lists(_num(0.0, 8.0), min_size=n, max_size=n)))
    solar = np.array(draw(st.lists(_num(0.0, 12.0), min_size=n, max_size=n)))
    capacity = draw(_num(1.0, 40.0))
    lo = draw(_num(0.0, 0.3)) * capacity
    hi = draw(_num(0.7, 1.0)) * capacity
    initial = draw(_num(lo, hi))
    battery = {
        "name": "b",
        "capacity_kwh": capacity,
        "initial_soc_kwh": initial,
        "min_soc_kwh": lo,
        "max_soc_kwh": hi,
        "max_charge_kw": draw(_num(0.5, 20.0)),
        "max_discharge_kw": draw(_num(0.5, 20.0)),
        "charge_efficiency": draw(_num(0.8, 0.99)),
        "discharge_efficiency": draw(_num(0.8, 0.99)),
        "charge_cost": 0.005,
        "discharge_cost": draw(_num(0.005, 0.1)),
        "salvage_value": draw(_num(0.0, 0.3)),
    }
    # Import limit always covers the load, so every generated site is
    # feasible without the battery.
    import_limit = float(load.max()) + draw(_num(0.0, 30.0))
    export_limit = draw(_num(0.0, 30.0))
    adequacy = None
    if draw(st.booleans()):
        earliest = draw(st.integers(min_value=0, max_value=n - 1))
        deadline = draw(st.integers(min_value=earliest, max_value=n - 1))
        adequacy = {
            "name": "hws",
            "max_power_kw": draw(_num(0.5, 5.0)),
            "target_kwh": draw(_num(0.1, 10.0)),
            "earliest_period": earliest,
            "deadline_period": deadline,
            "shortfall_price": draw(st.sampled_from([0.05, 0.5, 20.0])),
        }
        # The import limit also covers the adequacy load at full power.
        import_limit += adequacy["max_power_kw"]
    sheddable = None
    if draw(st.booleans()):
        sheddable = {
            "name": "pool",
            "forecast_kw": np.array(
                draw(st.lists(_num(0.0, 3.0), min_size=n, max_size=n))
            ),
            "shed_cost": draw(_num(0.01, 1.0)),
            "min_fraction": draw(_num(0.0, 1.0)),
        }
        import_limit += float(sheddable["forecast_kw"].max())
    thermal = None
    if draw(st.booleans()):
        power = draw(_num(0.5, 4.0))
        rate = draw(_num(1.0, 8.0))
        decay = draw(_num(0.0, 2.0))
        deadline = draw(st.integers(min_value=0, max_value=n - 1))
        start = draw(_num(30.0, 60.0))
        hours_to = width * (deadline + 1)
        # A target the load can reach at full power, so the guarantee holds.
        reach = min(start + (rate * power) * hours_to, 99.0)  # max_temperature_c
        thermal = {
            "name": "hws",
            "max_power_kw": power,
            "initial_temperature_c": start,
            "target_temperature_c": round(
                start + draw(_num(0.0, 0.9)) * (reach - start), 4
            ),
            "earliest_period": 0,
            "deadline_period": deadline,
            "heating_rate_c_per_kwh": rate,
            "idle_decay_c_per_hour": decay,
        }
        import_limit += power
    return {
        "adequacy": adequacy,
        "sheddable": sheddable,
        "thermal": thermal,
        "hours": np.full(n, width),
        "import_price": import_price,
        "export_price": export_price,
        "import_limit": import_limit,
        "export_limit": export_limit,
        "load": load,
        "solar": solar,
        "battery": battery,
    }


def _solve(site, **battery_overrides):
    return build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
        ),
        batteries=[BatteryConfig(**{**site["battery"], **battery_overrides})],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
        adequacy_loads=(
            [AdequacyLoadConfig(**site["adequacy"])] if site["adequacy"] else None
        ),
        sheddable_loads=(
            [SheddableLoadConfig(**site["sheddable"])] if site["sheddable"] else None
        ),
        thermal_loads=(
            [ThermalLoadConfig(**site["thermal"])] if site["thermal"] else None
        ),
    )


def _demand(site, plan):
    """The house load plus the adequacy, sheddable and thermal loads' draw."""
    out = site["load"].astype(float).copy()
    for a in plan.adequacy_loads or []:
        out = out + a.power_kw
    for sl in plan.sheddable_loads or []:
        out = out + sl.served_kw
    for tl in plan.thermal_loads or []:
        out = out + tl.power_kw
    return out


@SETTINGS
@given(
    sites(),
    _num(0.0, 1.0),
    st.frozensets(st.integers(0, 23)),
    st.booleans(),
    st.integers(min_value=0, max_value=3),
)
def test_a_spike_override_and_an_away_period_compose(
    site, share, away, available, until
):
    """#953 (Mark Purcell, IV&V #950): the spike override's floor and the
    away mask once built lb > ub on period 0 and the whole solve failed.
    Together they always solve, and the gate wins: an away period 0 does not
    discharge whatever the price; with period 0 free the override still pins
    it; an unavailable battery never arms the override at all (#779's prefix
    is read only then)."""
    b = site["battery"]
    sink = float(site["load"][0]) + site["export_limit"]
    rate = round(share * min(b["max_discharge_kw"], sink), 4)
    plan = _solve(
        site,
        spike_override_discharge_kw=rate,
        unavailable_period_indices=away,
        available=available,
        unavailable_until_period_index=None if available else (until or None),
    )
    assert plan.status == "optimal"
    if not available or 0 in away:
        assert plan.battery_discharge_kw[0] <= TOL
        assert plan.battery_charge_kw[0] <= TOL
    else:
        above_min = max(0.0, b["initial_soc_kwh"] - b["min_soc_kwh"])
        allowed = above_min * b["discharge_efficiency"] / site["hours"][0]
        assert abs(plan.battery_discharge_kw[0] - min(rate, allowed)) < 1e-5
    n = len(site["hours"])
    for t in range(n):
        # Unavailable means away for the whole horizon, or before #779's
        # return index when one is set.
        whole_or_prefix = not available and (until == 0 or t < until)
        if t in away or whole_or_prefix:
            assert plan.battery_discharge_kw[t] <= TOL, t
