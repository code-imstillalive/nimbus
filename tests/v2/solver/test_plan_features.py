"""Layer 1 of the rebuilt suite (#1837), round 2: the features the first
generator never built. The same-mutant comparison on `_build_plan_once`
(old 30/30, new 4/30, posted on #1837) found the survivors sat in code the
sites in `test_plan_invariants.py` never reach: the import release valve,
taper curves, a spike near Min SoC, an away period at t=0, the solar
confidence band and the terminal-value checks.

Each property here forces its feature to bind: a price that makes the LP
push a battery as hard as it can, or a load the grid limit can't cover, so a
wrong bound or coefficient changes the plan rather than hiding behind a
slack constraint.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from solver.network import build_plan
from test_plan_invariants import SETTINGS, TOL, _num, sites


def _solve(site, *, solar_band=None, **battery_overrides):
    lower, upper = solar_band if solar_band is not None else (None, None)
    return build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
        ),
        batteries=[BatteryConfig(**{**site["battery"], **battery_overrides})],
        solar=SolarConfig(forecast_kw=site["solar"], lower_kw=lower, upper_kw=upper),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
        sheddable_loads=(
            [SheddableLoadConfig(**site["sheddable"])] if site["sheddable"] else None
        ),
    )


@st.composite
def plain_sites(draw):
    """`sites()` without the adequacy and thermal loads (their penalties add
    objective terms the plan doesn't report on their own), and with the
    import limit sometimes below the load, so the release valve runs."""
    site = draw(sites())
    site["adequacy"] = None
    site["thermal"] = None
    if draw(st.booleans()):
        site["import_limit"] = round(
            float(site["load"].max()) * draw(_num(0.0, 0.9)), 4
        )
    return site


def _parts(site, plan):
    """What the plan's own flows cost at the input prices, plus the terms it
    reports separately."""
    b = site["battery"]
    h = site["hours"]
    grid = np.sum(
        plan.grid_import_kw * site["import_price"] * h
        - plan.grid_export_kw * site["export_price"] * h
    )
    wear = np.sum(
        plan.battery_charge_kw * b["charge_cost"] * h
        + plan.battery_discharge_kw * b["discharge_cost"] * h
    )
    salvage = -b["salvage_value"] * plan.battery_soc_kwh[-1]
    shed = sum(
        float(np.sum(s.shed_kw * site["sheddable"]["shed_cost"] * h))
        for s in plan.sheddable_loads or []
    )
    return (
        grid
        + wear
        + salvage
        + shed
        + plan.grid_import_excess_penalty_cost
        + plan.battery_loss_tiebreak_cost
        + plan.battery_charge_earliness_cost
        + plan.soc_penalty_cost
    )


@SETTINGS
@given(plain_sites())
def test_the_plan_costs_what_its_flows_cost(site):
    """The objective is the flows priced at the inputs (#390, #788): grid
    energy, battery wear, salvage of the energy left, shedding, and the
    release valve's markup, the loss tie-break and the charge earliness the
    plan reports. Any coefficient changed inside the LP shows up as a gap."""
    plan = _solve(site)
    assert plan.status == "optimal"
    assert abs(plan.total_cost - _parts(site, plan)) < 1e-6


@SETTINGS
@given(plain_sites())
def test_the_release_valve_takes_only_what_the_limit_cannot(site):
    """Import above the limit (#390) is a last resort: it is used only in a
    period where import within the limit is used up, its markup is
    max(10 x the dearest import price, $5) per kWh, and the plan's import is
    the total drawn, the excess included (so the balance closes on it)."""
    plan = _solve(site)
    h = site["hours"]
    excess = plan.grid_import_excess_kw
    assert (excess >= -TOL).all()
    assert (plan.grid_import_kw >= excess - TOL).all()
    within = plan.grid_import_kw - excess
    assert (within <= site["import_limit"] + 1e-5).all()
    for t in np.flatnonzero(excess > 1e-6):
        assert within[t] >= site["import_limit"] - 1e-5, t
    rate = max(10.0 * float(np.max(site["import_price"])), 5.0)
    assert abs(plan.grid_import_excess_penalty_cost - rate * np.sum(excess * h)) < 1e-6
    supply = (
        plan.grid_import_kw
        - plan.grid_export_kw
        + plan.battery_discharge_kw
        - plan.battery_charge_kw
        + plan.solar_used_kw
    )
    demand = site["load"] + sum(s.served_kw for s in plan.sheddable_loads or [])
    np.testing.assert_allclose(supply, demand, atol=TOL)


@SETTINGS
@given(sites())
def test_with_no_risk_aversion_the_plan_counts_the_forecast_as_given(site):
    """With the defaults, the solar and prices the LP counted on (#1797) are
    the inputs, even when the solar forecast carries a confidence band."""
    lower = site["solar"] * 0.5
    upper = site["solar"] * 1.5
    plan = _solve(site, solar_band=(lower, upper))
    np.testing.assert_allclose(plan.effective_solar_kw, site["solar"], atol=1e-9)
    np.testing.assert_allclose(
        plan.effective_import_price, site["import_price"], atol=1e-9
    )
    np.testing.assert_allclose(
        plan.effective_export_price, site["export_price"], atol=1e-9
    )


def _curve(lo, hi, flat_kw, end_kw, knee_share, rising):
    """A concave two-segment power curve over [lo, hi]: rising to a flat top
    (discharge tapering near empty) or flat then falling (charge tapering
    near full)."""
    knee = lo + knee_share * (hi - lo)
    if rising:
        return [(lo, end_kw), (knee, flat_kw), (hi, flat_kw)]
    return [(lo, flat_kw), (knee, flat_kw), (hi, end_kw)]


@st.composite
def push_sites(draw, direction):
    """A battery the prices push one way as hard as it can: paid to import
    (charge), or a rich export with room to sell (discharge). No load or
    solar, so nothing else competes for the flow."""
    n = draw(st.integers(min_value=2, max_value=12))
    width = draw(st.sampled_from([5 / 60, 0.25, 0.5, 1.0]))
    capacity = draw(_num(5.0, 40.0))
    lo = round(draw(_num(0.0, 0.2)) * capacity, 4)
    hi = round(draw(_num(0.8, 1.0)) * capacity, 4)
    flat = draw(_num(2.0, 15.0))
    curve = _curve(
        lo,
        hi,
        flat,
        round(flat * draw(_num(0.05, 0.9)), 4),
        draw(_num(0.2, 0.8)),
        rising=direction == "discharge",
    )
    start = lo if direction == "charge" else hi
    battery = {
        "name": "b",
        "capacity_kwh": capacity,
        "initial_soc_kwh": start,
        "min_soc_kwh": lo,
        "max_soc_kwh": hi,
        "max_charge_kw": flat,
        "max_discharge_kw": flat,
        "charge_efficiency": draw(_num(0.85, 0.99)),
        "discharge_efficiency": draw(_num(0.85, 0.99)),
        "charge_cost": 0.006,
        "discharge_cost": 0.006,
        "salvage_value": 0.0,
        f"{direction}_power_curve": curve,
    }
    if direction == "charge":
        imp, exp = np.full(n, -1.0), np.full(n, -2.0)
        battery["salvage_value"] = 0.5
    else:
        imp, exp = np.full(n, 5.0), np.full(n, 4.0)
    return {
        "hours": np.full(n, width),
        "import_price": imp,
        "export_price": exp,
        "import_limit": 2.0 * flat,
        "export_limit": 2.0 * flat,
        "load": np.zeros(n),
        "solar": np.zeros(n),
        "battery": battery,
        "sheddable": None,
        "curve": curve,
    }


def _flat_out(site, plan, direction):
    """Check every period against the cap at the SoC it starts from, and
    return the SoC that running flat out from the start would end on: the
    least of the inverter limit, the curve and the room left (charging), or
    of the inverter limit, the curve, the energy above Min SoC and the export
    limit (discharging). The steps are monotone in SoC, so no plan ends
    further than this."""
    b = site["battery"]
    socs, powers = zip(*site["curve"])
    h = site["hours"]
    flow = (
        plan.battery_charge_kw if direction == "charge" else plan.battery_discharge_kw
    )
    prev = b["initial_soc_kwh"]
    greedy = b["initial_soc_kwh"]
    for t in range(len(h)):
        assert flow[t] <= float(np.interp(prev, socs, powers)) + 1e-5, t
        prev = plan.battery_soc_kwh[t]
        curve = float(np.interp(greedy, socs, powers))
        if direction == "charge":
            room = (b["max_soc_kwh"] - greedy) / (b["charge_efficiency"] * h[t])
            rate = max(0.0, min(b["max_charge_kw"], curve, room))
            greedy += rate * b["charge_efficiency"] * h[t]
        else:
            left = (greedy - b["min_soc_kwh"]) * b["discharge_efficiency"] / h[t]
            cap = min(b["max_discharge_kw"], curve, left, site["export_limit"])
            greedy -= max(0.0, cap) / b["discharge_efficiency"] * h[t]
    return greedy


@SETTINGS
@given(push_sites("charge"))
def test_a_charge_taper_caps_charging_at_the_curve(site):
    """A charge power curve (v0.41.0): no period charges above the curve at
    the SoC it starts from, and a battery paid to charge ends as full as
    charging flat out would leave it."""
    plan = _solve(site)
    assert plan.status == "optimal"
    end = _flat_out(site, plan, "charge")
    assert abs(plan.battery_soc_kwh[-1] - end) < 1e-4


@SETTINGS
@given(push_sites("discharge"))
def test_a_discharge_taper_caps_discharging_at_the_curve(site):
    """A discharge power curve (v0.41.0): no period discharges above the curve
    at the SoC it starts from, and a battery paid to sell ends as empty as
    discharging flat out would leave it."""
    plan = _solve(site)
    assert plan.status == "optimal"
    end = _flat_out(site, plan, "discharge")
    assert abs(plan.battery_soc_kwh[-1] - end) < 1e-4


@SETTINGS
@given(push_sites("discharge"), _num(0.0, 0.5), _num(1.0, 3.0))
def test_a_spike_near_min_soc_discharges_only_what_is_left(site, above, factor):
    """The spike override (#1865) when the battery is nearly empty: period 0
    discharges what sits above Min SoC, never the full rate, and the plan
    stays feasible."""
    b = site["battery"]
    b["discharge_power_curve"] = None
    start = b["min_soc_kwh"] + above
    h0 = float(site["hours"][0])
    left = above * b["discharge_efficiency"] / h0
    rate = round(min(b["max_discharge_kw"], site["export_limit"], left * factor), 4)
    plan = _solve(site, initial_soc_kwh=start, spike_override_discharge_kw=rate)
    assert plan.status == "optimal"
    assert abs(plan.battery_discharge_kw[0] - min(rate, left)) < 1e-4
    assert plan.battery_soc_kwh[0] >= b["min_soc_kwh"] - 1e-4


@SETTINGS
@given(push_sites("discharge"), st.frozensets(st.integers(0, 11), min_size=1))
def test_an_away_period_holds_even_when_selling_pays(site, away):
    """`unavailable_period_indices` (#953), period 0 included: with export
    paying $4/kWh the battery would sell in every period it is allowed to,
    so a gated period that moves is a gating bug, not a choice."""
    site["battery"]["discharge_power_curve"] = None
    plan = _solve(site, unavailable_period_indices=away)
    assert plan.status == "optimal"
    n = len(site["hours"])
    for t in range(n):
        if t in away:
            assert plan.battery_discharge_kw[t] <= TOL, t
    free = [t for t in range(n) if t not in away]
    if free:
        assert plan.battery_discharge_kw[free].sum() > 1e-3


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_a_terminal_value_index_outside_the_grid_is_refused(n, past):
    """`terminal_value_period_indices` must lie on the grid: an index at or
    past n is refused, naming the grid's own range."""
    site = {
        "hours": np.full(n, 0.5),
        "import_price": np.full(n, 0.3),
        "export_price": np.full(n, 0.1),
        "import_limit": 10.0,
        "export_limit": 5.0,
        "load": np.ones(n),
        "solar": np.zeros(n),
        "sheddable": None,
        "battery": {
            "name": "b",
            "capacity_kwh": 10.0,
            "initial_soc_kwh": 5.0,
            "min_soc_kwh": 1.0,
            "max_soc_kwh": 9.0,
            "max_charge_kw": 3.0,
            "max_discharge_kw": 3.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.006,
            "discharge_cost": 0.006,
            "salvage_value": 0.0,
        },
    }
    with pytest.raises(ValueError, match=rf"\(0\.\.{n - 1}\)"):
        _solve(
            site,
            terminal_value_breakpoints=[(8.0, 0.1)],
            terminal_value_period_indices=[n + past],
        )
