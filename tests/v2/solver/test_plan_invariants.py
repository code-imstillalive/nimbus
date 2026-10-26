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
@given(sites())
def test_every_period_balances(site):
    """Import - export + discharge - charge + solar used = load (#390)."""
    plan = _solve(site)
    assert plan.status == "optimal"
    supply = (
        plan.grid_import_kw
        - plan.grid_export_kw
        + plan.battery_discharge_kw
        - plan.battery_charge_kw
        + plan.solar_used_kw
    )
    np.testing.assert_allclose(supply, _demand(site, plan), atol=TOL)
    np.testing.assert_allclose(
        plan.solar_used_kw + plan.solar_curtailed_kw, site["solar"], atol=TOL
    )


@SETTINGS
@given(sites())
def test_soc_follows_the_flows(site):
    """soc[t] is the end of period t: soc[t-1] + (charge * eff_c -
    discharge / eff_d) * hours[t], starting from the initial SoC."""
    plan = _solve(site)
    b = site["battery"]
    step = (
        plan.battery_charge_kw * b["charge_efficiency"]
        - plan.battery_discharge_kw / b["discharge_efficiency"]
    ) * site["hours"]
    expected = b["initial_soc_kwh"] + np.cumsum(step)
    np.testing.assert_allclose(plan.battery_soc_kwh, expected, atol=1e-4)


@SETTINGS
@given(sites())
def test_every_flow_and_soc_within_bounds(site):
    plan = _solve(site)
    b = site["battery"]
    for arr, cap in (
        (plan.battery_charge_kw, b["max_charge_kw"]),
        (plan.battery_discharge_kw, b["max_discharge_kw"]),
        (plan.grid_import_kw, site["import_limit"]),
        (plan.grid_export_kw, site["export_limit"]),
    ):
        assert (arr >= -TOL).all() and (arr <= cap + TOL).all()
    for soc in plan.battery_soc_kwh:
        assert b["min_soc_kwh"] - 1e-4 <= soc <= b["max_soc_kwh"] + 1e-4
    assert plan.soc_penalty_cost <= TOL


@SETTINGS
@given(sites())
def test_no_opposing_flows_in_one_period(site):
    """One grid connection, one current direction (#238, #1535)."""
    plan = _solve(site)
    assert (np.minimum(plan.battery_charge_kw, plan.battery_discharge_kw) <= TOL).all()
    assert (np.minimum(plan.grid_import_kw, plan.grid_export_kw) <= TOL).all()


@SETTINGS
@given(sites())
def test_a_battery_never_costs_more_than_leaving_it_idle(site):
    """Idle is a feasible plan, so the optimum can only be as cheap or
    cheaper."""
    plan = _solve(site)
    idle = _solve(site, available=False)
    assert idle.status == "optimal"
    assert plan.total_cost <= idle.total_cost + 1e-6
    assert (idle.battery_charge_kw <= TOL).all()
    assert (idle.battery_discharge_kw <= TOL).all()


@SETTINGS
@given(sites())
def test_an_adequacy_load_keeps_to_its_power_window_and_target(site):
    """Hot water and deferrables (#613): power in [0, max] and none before
    the earliest period; delivered + shortfall covers the target; a target
    the load can reach is met when a shortfall costs $20/kWh."""
    a = site["adequacy"]
    if a is None:
        return
    plan = _solve(site)
    assert plan.status == "optimal"
    (ap,) = plan.adequacy_loads
    hours = site["hours"]
    assert (ap.power_kw >= -TOL).all()
    assert (ap.power_kw <= a["max_power_kw"] + TOL).all()
    assert (ap.power_kw[: a["earliest_period"]] <= TOL).all()
    by_deadline = float(
        np.sum(
            ap.power_kw[: a["deadline_period"] + 1] * hours[: a["deadline_period"] + 1]
        )
    )
    assert abs(ap.delivered_by_deadline_kwh - by_deadline) < 1e-4
    assert ap.delivered_by_deadline_kwh + ap.shortfall_kwh >= a["target_kwh"] - 1e-4
    window = hours[a["earliest_period"] : a["deadline_period"] + 1].sum()
    if (
        a["shortfall_price"] >= 20.0
        and a["max_power_kw"] * window >= a["target_kwh"] + 1e-6
    ):
        assert ap.shortfall_kwh <= 1e-4


@SETTINGS
@given(sites(), _num(0.0, 1.0))
def test_a_battery_below_its_floor_never_sinks_further(site, depth):
    """Soft SoC bounds (#328, #781): a battery starting below its minimum
    is never planned lower than it starts, and the penalty is zero exactly
    when every period's SoC is within bounds."""
    b = site["battery"]
    start = b["min_soc_kwh"] * depth
    plan = _solve(site, initial_soc_kwh=start)
    assert plan.status == "optimal"
    soc = plan.battery_soc_kwh
    assert soc.min() >= min(start, b["min_soc_kwh"]) - 1e-4
    inside = bool(
        (soc >= b["min_soc_kwh"] - 1e-6).all()
        and (soc <= b["max_soc_kwh"] + 1e-6).all()
    )
    assert (plan.soc_penalty_cost <= 1e-6) == inside


@SETTINGS
@given(sites())
def test_buying_part_of_the_limit_prices_energy_at_the_buy_price(site):
    """LP duality (#662): in a period where the plan imports some but not all
    it could, and exports nothing, the energy balance's shadow price per kWh
    is that period's buy price. An adequacy load's marginal cost is that
    price times what it draws."""
    plan = _solve(site)
    hours = site["hours"]
    lam = np.array(
        [
            plan.duals.get(f"power_balance_t{t}", 0.0) / hours[t]
            for t in range(len(hours))
        ]
    )
    for t in range(len(hours)):
        imp = plan.grid_import_kw[t]
        if 1e-3 < imp < site["import_limit"] - 1e-3 and plan.grid_export_kw[t] <= 1e-6:
            # One more kW of demand is bought at this period's price.
            assert abs(lam[t] - site["import_price"][t]) < 1e-5, t
    if plan.adequacy_loads:
        (ap,) = plan.adequacy_loads
        expected = float(np.sum(lam * ap.power_kw * hours))
        assert abs(ap.marginal_cost - expected) < 1e-6


@SETTINGS
@given(sites())
def test_a_sheddable_load_serves_at_least_its_floor(site):
    """served + shed = forecast, and served never below min_fraction."""
    sh = site["sheddable"]
    if sh is None:
        return
    plan = _solve(site)
    (sp,) = plan.sheddable_loads
    np.testing.assert_allclose(sp.served_kw + sp.shed_kw, sh["forecast_kw"], atol=TOL)
    assert (sp.shed_kw >= -TOL).all()
    assert (sp.served_kw >= sh["min_fraction"] * sh["forecast_kw"] - TOL).all()


@SETTINGS
@given(sites())
def test_a_thermal_load_follows_its_model_and_meets_its_target(site):
    """Hot water (#603): temperature[t] is the end of period t,
    temperature[t-1] + rate x power x hours - decay x hours x idle share, where
    the idle share is 1 - power / max; the target is met by the deadline; power
    stays within [0, max]."""
    th = site["thermal"]
    if th is None:
        return
    plan = _solve(site)
    assert plan.status == "optimal"
    (tp,) = plan.thermal_loads
    hours = site["hours"]
    pw = tp.power_kw
    assert (pw >= -TOL).all() and (pw <= th["max_power_kw"] + TOL).all()
    idle = 1.0 - pw / th["max_power_kw"]
    step = (
        th["heating_rate_c_per_kwh"] * pw * hours
        - th["idle_decay_c_per_hour"] * hours * idle
    )
    expected = th["initial_temperature_c"] + np.cumsum(step)
    np.testing.assert_allclose(tp.temperature_c, expected, atol=1e-3)
    assert tp.temperature_c[th["deadline_period"]] >= th["target_temperature_c"] - 1e-3


@SETTINGS
@given(
    sites(), st.integers(min_value=0, max_value=24), st.frozensets(st.integers(0, 23))
)
def test_a_battery_that_is_away_neither_charges_nor_discharges(site, until, away):
    """An away battery (#467, #779, #953): `available=False` with
    `unavailable_until_period_index` gates that prefix of periods (it is read
    only when the battery is unavailable); `unavailable_period_indices` adds
    a per-period mask. In a gated period the battery is idle and its SoC is
    unchanged."""
    plan = _solve(
        site,
        available=until == 0,
        unavailable_until_period_index=until or None,
        unavailable_period_indices=away,
    )
    assert plan.status == "optimal"
    n = len(site["hours"])
    gated = [t for t in range(n) if t < until or t in away]
    for t in gated:
        assert plan.battery_charge_kw[t] <= TOL and plan.battery_discharge_kw[t] <= TOL
        before = (
            site["battery"]["initial_soc_kwh"]
            if t == 0
            else plan.battery_soc_kwh[t - 1]
        )
        assert abs(plan.battery_soc_kwh[t] - before) < 1e-4


@SETTINGS
@given(sites(), _num(0.0, 1.0))
def test_an_armed_price_spike_discharges_at_its_rate_now(site, share):
    """The spike override (#567, #694, #1865): period 0 discharges the set
    rate, down to Min SoC and never below it, and does not charge. The rate
    is kept within what period 0 can take (house load plus the export limit),
    so the energy has somewhere to go."""
    b = site["battery"]
    sink = float(site["load"][0]) + site["export_limit"]
    rate = round(share * min(b["max_discharge_kw"], sink), 4)
    plan = _solve(site, spike_override_discharge_kw=rate)
    assert plan.status == "optimal"
    above_min = max(0.0, b["initial_soc_kwh"] - b["min_soc_kwh"])
    allowed = above_min * b["discharge_efficiency"] / site["hours"][0]
    assert abs(plan.battery_discharge_kw[0] - min(rate, allowed)) < 1e-5
    assert plan.battery_charge_kw[0] <= TOL


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
