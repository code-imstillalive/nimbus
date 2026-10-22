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

import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp, network
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    MIN_CHARGE_DISCHARGE_COST_SPREAD,
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    DegenerateConfigError,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW, build_plan
from solver.p2p_export import realized_export_bonus_credit
from test_network_paths import _idle_battery
from test_plan_invariants import SETTINGS, TOL, _num, sites


def _solve(site, *, solar_band=None, plan_options=None, **battery_overrides):
    lower, upper = solar_band if solar_band is not None else (None, None)
    return build_plan(
        **(plan_options or {}),
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


def _solve_with_adequacy(site):
    from test_plan_invariants import _solve as solve_all

    return solve_all(site)


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
    # Cycle wear (Track B2): $/kWh of throughput in either direction.
    wear += b.get("degradation_cost_per_kwh", 0.0) * np.sum(
        (plan.battery_charge_kw + plan.battery_discharge_kw) * h
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
@given(plain_sites(), st.sampled_from([0.0, 0.01, 0.05, 0.3]))
def test_the_plan_costs_what_its_flows_cost(site, degradation):
    """The objective is the flows priced at the inputs (#390, #788): grid
    energy, battery wear (cycle wear too, at any rate, zero included),
    salvage of the energy left, shedding, and the release valve's markup,
    the loss tie-break and the charge earliness the plan reports. Any
    coefficient changed inside the LP shows up as a gap."""
    site["battery"]["degradation_cost_per_kwh"] = degradation
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
    # A load above the limit is planned through the valve, not refused: the
    # whole horizon once went infeasible for 46 minutes over one transient.
    assert plan.status == "optimal"
    h = site["hours"]
    excess = plan.grid_import_excess_kw
    assert abs(plan.import_cap_breach_kwh - float(np.sum(excess * h))) < 1e-6
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
    n = draw(st.integers(min_value=1, max_value=12))
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
@given(st.sampled_from(["charge", "discharge"]), st.data())
def test_a_curve_flat_at_the_inverter_limit_is_no_curve(direction, data):
    """The power curves (household, 21 Aug: the CC->CV charge taper, the BMS
    caution near empty) are a ceiling under the inverter limit, never a
    loosening: a curve flat at the limit plans as well as no curve, within
    the solver's tie-break slack (#773; a few hundredths of a cent here) and
    to the kWh at the end. Not period by period: at a flat price, when it
    sells is a tie."""
    site = data.draw(push_sites(direction))
    b = site["battery"]
    flat = [
        (b["min_soc_kwh"], b["max_charge_kw"]),
        (b["max_soc_kwh"], b["max_charge_kw"]),
    ]
    with_flat = _solve(site, **{f"{direction}_power_curve": flat})
    without = _solve(site, **{f"{direction}_power_curve": None})
    assert with_flat.status == without.status == "optimal"
    assert with_flat.total_cost == pytest.approx(without.total_cost, rel=1e-4)
    assert with_flat.battery_soc_kwh[-1] == pytest.approx(
        without.battery_soc_kwh[-1], abs=1e-4
    )


@pytest.mark.parametrize(
    ("curve", "error"),
    [
        ([(5.0, 10.0)], ValueError),
        ([(80.0, 10.0), (5.0, 10.0), (100.0, 0.0)], ValueError),
        ([(10.0, 10.0), (100.0, 0.0)], ValueError),
        ([(5.0, 10.0), (99.0, 0.0)], ValueError),
        ([(5.0, 10.0), (100.0, -1.0)], ValueError),
        ([(5.0, 1.0), (50.0, 2.0), (100.0, 10.0)], DegenerateConfigError),
    ],
    ids=["one-point", "unsorted", "not-from-min", "not-to-max", "negative", "convex"],
)
def test_a_power_curve_that_cannot_be_a_ceiling_is_refused(curve, error):
    """A curve must run Min SoC to Max SoC in order, never below zero, and
    be concave: the LP takes the least of each segment's extended line, which
    is the curve only when the slopes never rise. A convex curve would be a
    silently wrong, non-binding bound, so it is refused outright."""
    battery = {
        "name": "b",
        "capacity_kwh": 100.0,
        "initial_soc_kwh": 40.0,
        "min_soc_kwh": 5.0,
        "max_soc_kwh": 100.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    with pytest.raises(error):
        BatteryConfig(**battery, charge_power_curve=curve)
    ok = [(5.0, 10.0), (80.0, 10.0), (90.0, 6.0), (100.0, 0.0)]
    assert BatteryConfig(**battery, charge_power_curve=ok).charge_power_curve == ok


def test_both_curves_on_a_full_day_solve_quickly():
    """The household's compute-cost concern (21 Aug): both curves on a
    288-period, five-minute day add rows, not variables, and the solve stays
    well under five seconds."""
    import time

    n = 288
    rng = np.random.default_rng(42)
    battery = BatteryConfig(
        name="b",
        capacity_kwh=122.2,
        initial_soc_kwh=61.0,
        min_soc_kwh=2.4,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.10,
        charge_power_curve=[(2.4, 40.0), (97.8, 40.0), (122.2, 2.0)],
        discharge_power_curve=[(2.4, 3.0), (18.3, 40.0), (122.2, 40.0)],
    )
    started = time.perf_counter()
    plan = build_plan(
        periods=PeriodGrid(np.full(n, 5 / 60)),
        grid=GridConfig(
            import_price=0.15 + 0.10 * rng.random(n),
            export_price=0.05 + 0.05 * rng.random(n),
            import_limit_kw=40.0,
            export_limit_kw=40.0,
        ),
        batteries=[battery],
        solar=SolarConfig(
            forecast_kw=np.clip(10.0 * np.sin(np.linspace(0, np.pi, n)), 0, None)
        ),
        loads=[],
    )
    assert plan.status == "optimal"
    assert time.perf_counter() - started < 5.0


@SETTINGS
@given(push_sites("discharge"), _num(0.0, 0.5), _num(1.0, 3.0))
def test_a_spike_near_min_soc_discharges_only_what_is_left(site, above, factor):
    """The spike override (#567, #1865) when the battery is nearly empty:
    period 0 discharges what sits above Min SoC, never the full rate, and the
    plan stays feasible (the override's pin once contradicted the wash-trade
    guard here and the plan came back infeasible)."""
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


@SETTINGS
@given(plain_sites())
def test_a_battery_reports_the_headroom_its_plan_leaves(site):
    """The flex signals (#495): a battery can still move up by its charge
    limit less what it charges plus what it discharges, and down the other
    way round, in every period."""
    plan = _solve(site)
    b = site["battery"]
    (sig,) = plan.battery_signals
    np.testing.assert_allclose(
        sig.available_up_kw,
        b["max_charge_kw"] - plan.battery_charge_kw + plan.battery_discharge_kw,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        sig.available_down_kw,
        b["max_discharge_kw"] - plan.battery_discharge_kw + plan.battery_charge_kw,
        atol=1e-9,
    )


@SETTINGS
@given(push_sites("charge"), _num(0.5, 1.5))
def test_two_batteries_on_one_charger_share_its_limit(site, share):
    """A shared charger (#467): two batteries in one group never move more
    than its limit together, and when charging pays they use all of it
    while either still has room."""
    b = dict(site["battery"])
    b["charge_power_curve"] = None
    ceiling = round(b["max_charge_kw"] * share, 4)
    pair = [
        BatteryConfig(
            **{
                **b,
                "name": name,
                "shared_charger_group": "g",
                "shared_charger_max_kw": ceiling,
            }
        )
        for name in ("a", "c")
    ]
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=4.0 * b["max_charge_kw"],
            export_limit_kw=site["export_limit"],
        ),
        batteries=pair,
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    together = sum(pb.charge_kw + pb.discharge_kw for pb in plan.batteries)
    assert (together <= ceiling + 1e-5).all()
    h = site["hours"]
    room = sum(b["max_soc_kwh"] - pb.soc_kwh for pb in plan.batteries)
    # The first period: both start empty, so the charger's limit binds unless
    # the batteries' own limits or their room bind first.
    first = min(
        ceiling,
        2 * b["max_charge_kw"],
        2 * (b["max_soc_kwh"] - b["initial_soc_kwh"]) / (b["charge_efficiency"] * h[0]),
    )
    assert abs(together[0] - first) < 1e-4
    assert (room >= -1e-4).all()


@SETTINGS
@given(sites(), st.integers(min_value=1, max_value=23), _num(0.01, 0.2))
def test_a_short_weather_forecast_holds_its_last_value(site, known, coeff):
    """Ambient coupling (#481): an outdoor forecast shorter than the plan
    holds its last value to the end, so the plan is the one a forecast padded
    that way by hand gives."""
    th = site["thermal"]
    if th is None:
        return
    n = len(site["hours"])
    known = min(known, n)
    ambient = tuple(round(5.0 + 2.0 * i, 4) for i in range(known))
    padded = ambient + (ambient[-1],) * (n - known)

    def plan_with(series):
        return build_plan(
            periods=PeriodGrid(site["hours"]),
            grid=GridConfig(
                import_price=site["import_price"],
                export_price=site["export_price"],
                import_limit_kw=site["import_limit"],
                export_limit_kw=site["export_limit"],
            ),
            batteries=[BatteryConfig(**site["battery"])],
            solar=SolarConfig(forecast_kw=site["solar"]),
            loads=[LoadConfig(name="house", forecast_kw=site["load"])],
            thermal_loads=[
                ThermalLoadConfig(**th, loss_coeff_per_h=coeff, ambient_c=series)
            ],
        )

    short, full = plan_with(ambient), plan_with(padded)
    assert short.status == full.status == "optimal"
    np.testing.assert_allclose(
        short.thermal_loads[0].temperature_c,
        full.thermal_loads[0].temperature_c,
        atol=1e-6,
    )
    assert abs(short.total_cost - full.total_cost) < 1e-6


@SETTINGS
@given(
    st.integers(min_value=2, max_value=16),
    st.sampled_from([5 / 60, 0.25, 0.5, 1.0]),
    st.integers(min_value=0, max_value=4),
    _num(0.5, 4.0),
    st.integers(min_value=1, max_value=16),
    _num(0.05, 5.0),
)
def test_a_deferral_threshold_runs_a_load_as_early_as_it_can(
    n, width, earliest, power, periods, threshold
):
    """`min_deferral_saving_dollars` (#769): with the same price all day
    there is nothing to save by waiting, so the load runs from its earliest
    period at full power until its target is met. (A load runs on or off at
    full power, so the target here is whole periods of it.)"""
    earliest = min(earliest, n - 1)
    hours = np.full(n, width)
    periods = min(periods, n - earliest)
    target = power * width * periods
    plan = build_plan(
        periods=PeriodGrid(hours),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 0.05),
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
                name="pool",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=earliest,
                deadline_period=n - 1,
                shortfall_price=20.0,
                min_deferral_saving_dollars=threshold,
            )
        ],
    )
    assert plan.status == "optimal"
    (ap,) = plan.adequacy_loads
    delivered = np.cumsum(ap.power_kw * hours)
    for t in range(n):
        reach = power * width * max(0, t - earliest + 1)
        assert abs(delivered[t] - min(target, reach)) < 1e-4, t


def _tiered_value(above, tiers):
    """What a concave terminal value pays for `above` kWh over Min SoC: the
    dearest tier first, then the next."""
    total = 0.0
    for width, rate in tiers:
        take = min(width, max(0.0, above))
        total += take * rate
        above -= take
    return total


@SETTINGS
@given(
    plain_sites(),
    st.lists(_num(0.1, 1.0), min_size=1, max_size=3),
    st.lists(_num(0.0, 0.5), min_size=3, max_size=3),
    st.frozensets(st.integers(0, 23), max_size=3),
)
def test_a_tiered_terminal_value_is_counted_at_each_of_its_periods(
    site, shares, rates, extra
):
    """Terminal value tiers (v0.28.0, v0.42.0): the SoC above Min SoC at the last period
    is worth the tiers, dearest first, in place of the flat salvage; each
    extra period listed counts the same curve, split evenly between them."""
    b = site["battery"]
    span = b["max_soc_kwh"] - b["min_soc_kwh"]
    if span <= 1e-3:
        return
    widths = [span * s / sum(shares) for s in shares]
    widths[-1] = span - sum(widths[:-1])
    tiers = list(zip(widths, sorted(rates[: len(widths)], reverse=True), strict=True))
    n = len(site["hours"])
    extra = sorted(t for t in extra if t < n - 1)
    plan = _solve(
        site,
        terminal_value_breakpoints=tiers,
        terminal_value_period_indices=extra + [n - 1] if extra else None,
    )
    assert plan.status == "optimal"
    soc = plan.battery_soc_kwh
    value = _tiered_value(soc[-1] - b["min_soc_kwh"], tiers)
    for t in extra:
        value += _tiered_value(soc[t] - b["min_soc_kwh"], tiers) / len(extra)
    flat = -b["salvage_value"] * soc[-1]
    assert abs(plan.total_cost - (_parts(site, plan) - flat - value)) < 1e-6


def _checkpoint_plan(n, export, indices):
    tiers = [(6.0, 0.60), (16.0, 0.25), (16.0, 0.05)]
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.50),
            export_price=np.full(n, export),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=40.0,
                initial_soc_kwh=40.0,
                min_soc_kwh=2.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.97,
                discharge_efficiency=0.97,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
                terminal_value_breakpoints=tiers,
                terminal_value_period_indices=indices,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 0.5))],
    )


@SETTINGS
@given(st.integers(8, 16), st.data(), _num(0.12, 0.2))
def test_a_terminal_checkpoint_holds_energy_at_its_boundary(n, data, export):
    """Household, 22 Aug 2026 (chart evidence): the plan kept selling for
    fifty minutes past the P2P window at an unchanged price, because the
    terminal value only protected the horizon's last period and every other
    day boundary looked like the end of the world. A checkpoint at a
    boundary makes the battery hold its dearest tier there that it would
    otherwise have sold (export here pays more than the cheaper tiers but
    less than the dearest). `None` is the last period alone, exactly."""
    t = data.draw(st.integers(n // 2, n - 3))
    without = _checkpoint_plan(n, export, None)
    explicit = _checkpoint_plan(n, export, [n - 1])
    held = _checkpoint_plan(n, export, [t, n - 1])
    assert without.status == explicit.status == held.status == "optimal"
    np.testing.assert_array_equal(explicit.battery_soc_kwh, without.battery_soc_kwh)
    assert explicit.total_cost == without.total_cost
    assert float(held.battery_soc_kwh[t]) > float(without.battery_soc_kwh[t]) + 1.0


@pytest.mark.parametrize(
    ("indices", "tiers"),
    [([5], None), ([-1, 5], [(10.0, 0.1)]), ([3, 3], [(10.0, 0.1)])],
    ids=["without-tiers", "negative", "duplicate"],
)
def test_terminal_checkpoints_that_mean_nothing_are_refused(indices, tiers):
    """Checkpoints need the tier curve to count, a real period, and each
    period once."""
    with pytest.raises(ValueError):
        BatteryConfig(
            name="b",
            capacity_kwh=12.0,
            initial_soc_kwh=6.0,
            min_soc_kwh=2.0,
            max_soc_kwh=12.0,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.0,
            terminal_value_breakpoints=tiers,
            terminal_value_period_indices=indices,
        )


@SETTINGS
@given(
    plain_sites(),
    st.sampled_from([None, 0.0, 0.01, 0.05]),
)
def test_charge_earliness_costs_what_its_rate_says(site, budget):
    """The charge-earliness tie-break (#692, #1480): each kWh charged in a
    period costs budget / horizon hours x the hours elapsed before it, and
    the plan reports exactly that (zero with a zero budget). The scorer
    sets it apart from a real LP-vs-evaluator gap (#1480: $0.21 of 28 Sep's
    $1.84 `j_star_path_delta`)."""
    options = {} if budget is None else {"battery_charge_earliness_budget_kw": budget}
    plan = _solve(site, plan_options=options)
    h = site["hours"]
    elapsed = np.concatenate(([0.0], np.cumsum(h)[:-1]))
    used = DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW if budget is None else budget
    rate = used / float(np.sum(h))
    expected = float(np.sum(rate * elapsed * h * plan.battery_charge_kw))
    assert abs(plan.battery_charge_earliness_cost - expected) < 1e-9


@SETTINGS
@given(push_sites("discharge"), _num(0.05, 1.0))
def test_a_battery_below_its_floor_never_discharges(site, depth):
    """Below Min SoC (#328, #781): even with selling at $4/kWh, a battery
    that starts under its floor discharges nothing, in any period."""
    b = site["battery"]
    b["discharge_power_curve"] = None
    b["min_soc_kwh"] = round(max(b["min_soc_kwh"], 1.0), 4)
    start = round(b["min_soc_kwh"] * (1.0 - depth), 4)
    plan = _solve(site, initial_soc_kwh=start)
    assert plan.status == "optimal"
    assert (plan.battery_discharge_kw <= TOL).all()


@SETTINGS
@given(push_sites("discharge"), _num(0.5, 1.5))
def test_two_batteries_on_one_charger_share_it_discharging(site, share):
    """A shared charger (#467) caps discharging too: two full batteries paid
    to sell move no more than its limit together in any period, and over the
    plan sell the lesser of what the charger can carry and what they hold
    above Min SoC."""
    b = dict(site["battery"])
    b["discharge_power_curve"] = None
    ceiling = round(b["max_discharge_kw"] * share, 4)
    pair = [
        BatteryConfig(
            **{
                **b,
                "name": name,
                "shared_charger_group": "g",
                "shared_charger_max_kw": ceiling,
            }
        )
        for name in ("a", "c")
    ]
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=4.0 * b["max_discharge_kw"],
        ),
        batteries=pair,
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    together = sum(pb.charge_kw + pb.discharge_kw for pb in plan.batteries)
    assert (together <= ceiling + 1e-5).all()
    h = site["hours"]
    held = 2 * (b["initial_soc_kwh"] - b["min_soc_kwh"]) * b["discharge_efficiency"]
    carried = min(ceiling, 2 * b["max_discharge_kw"]) * float(np.sum(h))
    sold = float(np.sum(together * h))
    assert abs(sold - min(held, carried)) < 1e-4


@SETTINGS
@given(
    st.lists(
        st.tuples(st.integers(1, 4), st.integers(1, 6), _num(0.1, 12.0)),
        min_size=1,
        max_size=3,
    ),
    _num(0.3, 3.0),
    st.sampled_from([0.5, 1.0]),
    st.booleans(),
)
def test_each_window_delivers_its_own_target_inside_its_own_periods(
    shapes, power, width, semi
):
    """#612 ("plan_forecast is 0.0 for 10-13 Sep ... later days' battery
    plans ignore the daily 2 kWh"). A load with several daily windows,
    separated by gaps: each window delivers its OWN target inside its OWN
    periods, or all it can reach there; one window's surplus never counts
    toward another's. Nothing runs between windows, even where the grid
    pays to take power (a negative price in every gap). An unreachable window
    is a priced shortfall, not an infeasible plan, and leaves every other
    window served. The reported shortfall is the nearest window's alone
    (#739, Mark Purcell: a future day's unreachable target leaked into
    today's "will miss target"). Run once (#616, the default), a window
    runs whole periods at full power: a target between two whole numbers of
    periods is rounded up or left a priced shortfall, whichever costs less."""
    windows = []
    t = 0
    for gap, length, target in shapes:
        t += gap
        windows.append(
            AdequacyWindow(
                earliest_period=t, deadline_period=t + length - 1, target_kwh=target
            )
        )
        t += length
    n = t + 2
    inside = np.zeros(n, dtype=bool)
    for w in windows:
        inside[w.earliest_period : w.deadline_period + 1] = True
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.where(inside, 0.30, -0.10),
            export_price=np.zeros(n),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=power,
                target_kwh=windows[0].target_kwh,
                earliest_period=windows[0].earliest_period,
                deadline_period=windows[0].deadline_period,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=tuple(windows),
            )
        ],
        adequacy_semi_continuous=semi,
    )
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    step = power * width
    for w in windows:
        span = slice(w.earliest_period, w.deadline_period + 1)
        reach = power * width * (w.deadline_period - w.earliest_period + 1)
        delivered = float(np.sum(al.power_kw[span] * width))
        if not semi or w.target_kwh >= reach:
            assert delivered == pytest.approx(min(w.target_kwh, reach), abs=1e-4)
            continue
        down = step * np.floor(w.target_kwh / step + 1e-9)
        up = min(down + step, reach)
        short = (w.target_kwh - down) * DEFAULT_ADEQUACY_SHORTFALL_PRICE
        run = (up - down) * 0.30
        if short < run - 1e-6:
            assert delivered == pytest.approx(down, abs=1e-4)
        elif run < short - 1e-6:
            assert delivered == pytest.approx(up, abs=1e-4)
        else:
            assert min(abs(delivered - down), abs(delivered - up)) <= 1e-4
    assert np.all(al.power_kw[~inside] <= 1e-9)
    first = windows[0]
    span0 = slice(first.earliest_period, first.deadline_period + 1)
    delivered0 = float(np.sum(al.power_kw[span0] * width))
    assert al.shortfall_kwh == pytest.approx(
        max(0.0, first.target_kwh - delivered0), abs=1e-4
    )


@SETTINGS
@given(
    st.integers(min_value=4, max_value=16),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(0.5, 3.0),
    st.integers(min_value=1, max_value=2),
    st.integers(min_value=1, max_value=2),
)
def test_each_window_meets_a_target_it_can_reach(n, width, power, k1, k2):
    """Several windows (#1059): each window's target is whole periods of
    full power inside it, so with a dear shortfall every one is met and no
    shortfall is planned."""
    half = n // 2
    windows = (
        AdequacyWindow(
            earliest_period=0,
            deadline_period=half - 1,
            target_kwh=power * width * min(k1, half),
        ),
        AdequacyWindow(
            earliest_period=half,
            deadline_period=n - 1,
            target_kwh=power * width * min(k2, n - half),
        ),
    )
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 0.05),
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
                name="pool",
                max_power_kw=power,
                target_kwh=sum(w.target_kwh for w in windows),
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=20.0,
                windows=windows,
            )
        ],
    )
    assert plan.status == "optimal"
    (ap,) = plan.adequacy_loads
    assert ap.shortfall_kwh <= 1e-4
    hours = np.full(n, width)
    for w in windows:
        span = slice(w.earliest_period, w.deadline_period + 1)
        assert float(np.sum(ap.power_kw[span] * hours[span])) >= w.target_kwh - 1e-4


@pytest.mark.parametrize("width", [5 / 60, 0.5, 1.0])
@pytest.mark.parametrize("above", [0.0, 0.1, 0.4])
def test_a_one_period_spike_near_min_soc_stays_feasible(width, above):
    """The spike override (#1865) on a one-period plan, the shortest a solve
    can be: period 0 discharges only what sits above Min SoC, never the set
    rate, so the plan stays feasible."""
    plan = build_plan(
        periods=PeriodGrid(np.array([width])),
        grid=GridConfig(
            import_price=np.array([5.0]),
            export_price=np.array([4.0]),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=10.0,
                initial_soc_kwh=1.0 + above,
                min_soc_kwh=1.0,
                max_soc_kwh=9.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.0,
                spike_override_discharge_kw=10.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(1)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(1))],
    )
    assert plan.status == "optimal"
    assert abs(plan.battery_discharge_kw[0] - above * 0.95 / width) < 1e-5


@SETTINGS
@given(
    st.integers(min_value=4, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(0.5, 3.0),
    _num(0.02, 0.2),
)
def test_with_no_threshold_a_load_waits_for_any_real_saving(n, width, power, cut):
    """`min_deferral_saving_dollars` defaults to none (#769): when the later
    half of the day is cheaper by even a few cents, a load with no threshold
    runs there, not at its earliest period."""
    half = n // 2
    price = np.array([0.3] * half + [0.3 - cut] * (n - half))
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=price,
            export_price=np.full(n, 0.0),
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
                name="pool",
                max_power_kw=power,
                target_kwh=power * width,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=20.0,
            )
        ],
    )
    assert plan.status == "optimal"
    (ap,) = plan.adequacy_loads
    assert float(np.sum(ap.power_kw[:half])) <= 1e-6
    assert abs(float(np.sum(ap.power_kw * width)) - power * width) < 1e-6


@SETTINGS
@given(
    st.integers(min_value=4, max_value=10),
    st.sampled_from([0.25, 0.5, 1.0]),
    st.integers(min_value=0, max_value=2),
    _num(0.5, 2.0),
)
def test_the_main_battery_never_charges_inside_a_committed_export_window(
    n, width, start, committed
):
    """A P2P fixed export window (household finding, 2026-08-20): the first battery's charging is shut
    in every committed period, even when the sun is up and charging pays, so
    the committed export can't be bought back through it. A second battery
    is not gated."""
    window = range(start, min(start + 2, n))
    fixed = np.full(n, np.nan)
    for t in window:
        fixed[t] = committed

    def battery(name):
        return BatteryConfig(
            name=name,
            capacity_kwh=20.0,
            initial_soc_kwh=2.0,
            min_soc_kwh=1.0,
            max_soc_kwh=19.0,
            max_charge_kw=3.0,
            max_discharge_kw=3.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.006,
            discharge_cost=0.006,
            salvage_value=0.5,
        )

    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 0.05),
            import_limit_kw=10.0,
            export_limit_kw=10.0,
            fixed_export_kw=fixed,
        ),
        batteries=[battery("main"), battery("second")],
        solar=SolarConfig(forecast_kw=np.full(n, 12.0)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
    )
    assert plan.status == "optimal"
    main, second = plan.batteries
    for t in window:
        assert main.charge_kw[t] <= TOL, t
    # The ungated battery does charge from the spare sun in the window.
    assert max(second.charge_kw[t] for t in window) > 1e-3


@SETTINGS
@given(
    st.integers(min_value=2, max_value=10),
    st.sampled_from([0.25, 0.5, 1.0]),
    st.integers(min_value=0, max_value=9),
    st.sampled_from([0.3, 0.7, 1.0, 1.2]),
)
def test_a_departure_target_is_met_when_reachable_and_warned_only_when_not(
    n, width, by, share
):
    """An EV departure target (#563, unreachable since #1818): a target at
    or under what full-power charging can reach by its period is met there,
    with no 'unreachable' warning. One above it is charged as far as it can
    go, the plan stays solvable, and the warning names it. Charging costs
    money here, so only the target makes the battery charge."""
    by = min(by, n - 1)
    hours = np.full(n, width)
    initial, cap, rate, eff = 2.0, 40.0, 3.0, 0.95
    reachable = min(cap, initial + rate * eff * width * (by + 1))
    target = round(initial + share * (reachable - initial), 6)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("solver.network")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        plan = build_plan(
            periods=PeriodGrid(hours),
            grid=GridConfig(
                import_price=np.full(n, 0.4),
                export_price=np.full(n, 0.0),
                import_limit_kw=20.0,
                export_limit_kw=0.0,
            ),
            batteries=[
                BatteryConfig(
                    name="ev",
                    capacity_kwh=cap,
                    initial_soc_kwh=initial,
                    min_soc_kwh=0.0,
                    max_soc_kwh=cap,
                    max_charge_kw=rate,
                    max_discharge_kw=rate,
                    charge_efficiency=eff,
                    discharge_efficiency=eff,
                    charge_cost=0.006,
                    discharge_cost=0.006,
                    salvage_value=0.0,
                    must_have_soc_by_period_index=by,
                    must_have_soc_kwh=target,
                )
            ],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        )
    finally:
        log.removeHandler(handler)
    assert plan.status == "optimal"
    soc = plan.battery_soc_kwh[by]
    warned = any("ev" in r.getMessage() and "reach" in r.getMessage() for r in records)
    if target <= reachable + 1e-9:
        assert soc >= target - 1e-4
        assert not warned
    else:
        assert abs(soc - reachable) < 1e-4
        assert warned


@SETTINGS
@given(plain_sites(), st.integers(min_value=0, max_value=23), _num(0.5, 15.0))
def test_a_short_p2p_block_costs_its_penalty_on_every_missing_kwh(site, at, kw):
    """A committed P2P block (#1761): each kWh it falls short costs more than
    the release valve plus the best any kWh could earn, so the plan's cost is
    its flows plus that rate on every kWh the export misses the block by,
    whatever the plan chose."""
    n = len(site["hours"])
    fixed = np.full(n, np.nan)
    window = [t for t in range(at, min(at + 2, n))]
    for t in window:
        fixed[t] = kw
    site["export_limit"] = max(site["export_limit"], kw)
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
            fixed_export_kw=fixed,
        ),
        batteries=[BatteryConfig(**site["battery"])],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
        sheddable_loads=(
            [SheddableLoadConfig(**site["sheddable"])] if site["sheddable"] else None
        ),
    )
    assert plan.status == "optimal"
    h = site["hours"]
    excess_rate = max(10.0 * float(np.max(site["import_price"])), 5.0)
    rate = (
        excess_rate
        + float(np.max(np.abs(site["import_price"])))
        + float(np.max(np.abs(site["export_price"])))
    )
    short = sum(max(0.0, kw - plan.grid_export_kw[t]) * h[t] for t in window)
    assert abs(plan.total_cost - (_parts(site, plan) + rate * short)) < 1e-6


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_an_adequacy_deadline_past_the_plan_is_refused_naming_the_plan(n, past):
    """An adequacy load whose deadline lies at or past the plan's last
    period is refused before solving, and the message names the load, its
    deadline and the plan's own range."""
    deadline = n + past
    with pytest.raises(ValueError) as refused:
        build_plan(
            periods=PeriodGrid(np.full(n, 0.5)),
            grid=GridConfig(
                import_price=np.full(n, 0.3),
                export_price=np.full(n, 0.05),
                import_limit_kw=10.0,
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
                    name="pool",
                    max_power_kw=1.0,
                    target_kwh=1.0,
                    deadline_period=deadline,
                    shortfall_price=1.0,
                )
            ],
        )
    msg = str(refused.value)
    assert "'pool'" in msg and f"({deadline})" in msg and f"(0..{n - 1})" in msg


@SETTINGS
@given(plain_sites())
def test_the_loss_tie_break_costs_what_its_rate_says(site):
    """The round-trip loss tie-break (#731, #732, reported for #1480): each
    kWh charged costs the import
    price x (1 - charge efficiency), each kWh discharged the import price x
    (1 / discharge efficiency - 1), each capped at the 1c minimum spread, and
    the plan reports exactly that."""
    plan = _solve(site)
    b = site["battery"]
    h = site["hours"]
    price = site["import_price"]
    cap = MIN_CHARGE_DISCHARGE_COST_SPREAD
    charge_rate = np.minimum(price * (1.0 - b["charge_efficiency"]), cap)
    discharge_rate = np.minimum(price * (1.0 / b["discharge_efficiency"] - 1.0), cap)
    expected = float(
        np.sum(plan.battery_charge_kw * charge_rate * h)
        + np.sum(plan.battery_discharge_kw * discharge_rate * h)
    )
    assert abs(plan.battery_loss_tiebreak_cost - expected) < 1e-9


def _loss_battery(name, initial, eff, **over):
    return BatteryConfig(
        **{
            "name": name,
            "capacity_kwh": 40.0,
            "initial_soc_kwh": initial,
            "min_soc_kwh": 0.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 20.0,
            "max_discharge_kw": 20.0,
            "charge_efficiency": eff,
            "discharge_efficiency": eff,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            **over,
        }
    )


def _one_period(price, *batteries):
    return build_plan(
        periods=PeriodGrid(np.ones(1)),
        grid=GridConfig(
            import_price=np.full(1, price),
            export_price=np.full(1, -0.01),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=list(batteries),
        solar=SolarConfig(forecast_kw=np.zeros(1)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(1))],
    )


def _loss(price, rate):
    return min(price * rate, MIN_CHARGE_DISCHARGE_COST_SPREAD)


@SETTINGS
@given(_num(0.01, 0.8), _num(0.85, 0.99), _num(1.0, 15.0))
def test_the_loss_is_paid_in_the_plan_on_a_forced_charge(price, eff, stored):
    """#732 (Mark Purcell, greenlit for #731's EV-to-home wash trade): the
    loss is a cost the plan PAYS, not only one it reports. A charge forced
    by a deadline costs its grid energy, its charge cost and its loss at
    the import price x (1 - efficiency), capped at the 1c spread (#692's
    lesson: uncapped, it blocked free-solar charging)."""
    plan = _one_period(
        price,
        _loss_battery(
            "home",
            0.0,
            eff,
            must_have_soc_by_period_index=0,
            must_have_soc_kwh=stored,
        ),
    )
    assert plan.status == "optimal"
    drawn = stored / eff
    assert float(plan.batteries[0].charge_kw[0]) == pytest.approx(drawn, abs=1e-5)
    want = drawn * (price + 0.01 + _loss(price, 1.0 - eff))
    assert plan.total_cost == pytest.approx(want, abs=1e-5)


@SETTINGS
@given(_num(0.01, 0.8), _num(0.85, 0.99), _num(0.85, 0.99), _num(1.0, 12.0))
def test_a_battery_to_battery_transfer_pays_the_loss_on_both_legs(
    price, home_eff, ev_eff, stored
):
    """#731's shape: an EV made to give up energy in the same period a home
    battery must take it, with no grid flow between. The plan pays each
    leg's own loss: the EV's on discharge (price x (1/eff - 1)), the home's
    on charge (price x (1 - eff)), each capped, plus both flat costs."""
    moved = stored / home_eff
    drop = moved / ev_eff
    home = _loss_battery(
        "home", 0.0, home_eff, must_have_soc_by_period_index=0, must_have_soc_kwh=stored
    )
    ev = _loss_battery("ev", 30.0, ev_eff, max_soc_kwh=30.0 - drop)
    plan = _one_period(price, home, ev)
    assert plan.status == "optimal"
    by = {b.name: b for b in plan.batteries}
    assert float(by["ev"].discharge_kw[0]) == pytest.approx(moved, abs=1e-5)
    assert float(by["home"].charge_kw[0]) == pytest.approx(moved, abs=1e-5)
    assert float(plan.grid_import_kw[0]) == pytest.approx(0.0, abs=1e-5)
    want = moved * (
        0.02 + _loss(price, 1.0 / ev_eff - 1.0) + _loss(price, 1.0 - home_eff)
    )
    assert plan.total_cost == pytest.approx(want, abs=1e-5)


def _pool_plan(n, width, power, price, threshold, windows=None, earliest=0):
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=price,
            export_price=np.full(n, 0.0),
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
                name="pool",
                max_power_kw=power,
                target_kwh=power * width,
                earliest_period=earliest,
                deadline_period=n - 1,
                shortfall_price=20.0,
                min_deferral_saving_dollars=threshold,
                windows=windows,
            )
        ],
    )


@SETTINGS
@given(
    st.integers(min_value=4, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(0.5, 3.0),
    _num(0.02, 0.2),
    st.integers(min_value=0, max_value=3),
)
def test_a_deferral_threshold_blocks_a_saving_smaller_than_it(
    n, width, power, cut, earliest
):
    """`min_deferral_saving_dollars` (#769): when waiting for the cheaper half
    of the day saves less than the threshold, the load runs before it
    instead; with the threshold well under the saving, it waits. The window
    starts at period 0 or later, and its hours are measured from its own
    start."""
    half = n // 2
    earliest = min(earliest, half - 1)
    price = np.array([0.3] * half + [0.3 - cut] * (n - half))
    saving = cut * power * width
    window_h = width * (n - 1 - earliest)
    late_h = width * (half - earliest)
    # The lateness cost of waiting to `half` is threshold x late_h / window_h;
    # these thresholds put it just above the saving (1.1x), so a window
    # measured wrongly flips the decision, and at a hundredth of it.
    blocking = saving * 1.1 * window_h / late_h
    held = _pool_plan(n, width, power, price, blocking, earliest=earliest)
    assert held.status == "optimal"
    assert float(np.sum(held.adequacy_loads[0].power_kw[:half])) > 1e-6
    waits = _pool_plan(n, width, power, price, saving * 0.01, earliest=earliest)
    assert float(np.sum(waits.adequacy_loads[0].power_kw[:half])) <= 1e-6


def _deferral_plan(n, width, price, threshold, spans, runs, calibrated=False):
    """A 1 kW load whose windows are `spans`, each owing `runs` whole
    periods; `threshold` None leaves the field unset."""
    windows = tuple(
        AdequacyWindow(earliest_period=a, deadline_period=b, target_kwh=runs * width)
        for a, b in spans
    )
    extra = {} if threshold is None else {"min_deferral_saving_dollars": threshold}
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.asarray(price, dtype=float),
            export_price=np.zeros(n),
            import_limit_kw=10.0,
            export_limit_kw=10.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=1.0,
                target_kwh=runs * width,
                earliest_period=spans[0][0],
                deadline_period=spans[0][1],
                shortfall_price=20.0,
                windows=windows if len(spans) > 1 else None,
                **extra,
            )
        ],
        solve_options=lp.CalibratedOptions() if calibrated else None,
    )


@SETTINGS
@given(
    st.integers(1, 3),
    st.integers(0, 3),
    st.integers(0, 4),
    st.sampled_from([0.5, 1.0]),
    _num(0.05, 3.0),
    st.booleans(),
    st.booleans(),
)
def test_the_deferral_penalty_costs_its_stated_share_of_the_threshold(
    runs, start, slack, width, threshold, two, calibrated
):
    """#769 (Mark Purcell; household decision 15 Sep: "each load can have
    its own urgency then"). Each window prices lateness from its OWN
    opening at rate = (X / target) / window_hours, so the whole target at
    the deadline costs exactly X and at the opening nothing. At a flat
    price the load runs from each window's opening, and the plan costs that
    rate times the lateness it actually carries over the unset plan. A
    second window pulls to its own opening, not to the plan's start. The
    cost is a PRIMARY cost, on purpose allowed to beat real prices, so the
    calibrated solve (production's) charges exactly the same."""
    length = runs + slack
    spans = [(start, start + length - 1)]
    if two:
        spans.append((spans[0][1] + 2, spans[0][1] + 1 + length))
    n = spans[-1][1] + 2
    price = np.full(n, 0.20)
    base = _deferral_plan(n, width, price, None, spans, runs, calibrated)
    plan = _deferral_plan(n, width, price, threshold, spans, runs, calibrated)
    assert base.status == plan.status == "optimal"
    power = plan.adequacy_loads[0].power_kw
    want = 0.0
    for a, b in spans:
        np.testing.assert_allclose(power[a : a + runs], 1.0, atol=1e-6)
        np.testing.assert_allclose(power[a + runs : b + 1], 0.0, atol=1e-6)
        window_h = width * (b - a)
        if window_h > 0:
            rate = threshold / (runs * width) / window_h
            want += sum(rate * width * k * width for k in range(runs))
    assert plan.total_cost - base.total_cost == pytest.approx(want, abs=1e-6)


def test_a_whole_target_at_the_deadline_costs_exactly_the_threshold():
    """The promise as the household was given it, measured with nowhere
    else to go: a one-period target in a window whose every period but the
    deadline is priced out of reach, so it runs at the deadline and pays
    exactly X over the unset plan."""
    n, threshold, spans = 6, 0.75, [(0, 4)]
    price = np.array([5.0] * 4 + [0.20, 0.20])
    forced = _deferral_plan(n, 1.0, price, threshold, spans, 1)
    unset = _deferral_plan(n, 1.0, price, None, spans, 1)
    assert forced.status == unset.status == "optimal"
    assert float(forced.adequacy_loads[0].power_kw[4]) == pytest.approx(1.0)
    assert forced.total_cost - unset.total_cost == pytest.approx(threshold)


@SETTINGS
@given(st.lists(_num(0.02, 0.5), min_size=6, max_size=12), st.integers(1, 3))
def test_a_zero_threshold_is_the_unset_plan_exactly(prices, runs):
    """#769: 0 is the off switch, byte-identical to leaving it unset, so
    every install that never touches it plans as before."""
    n = len(prices)
    spans = [(0, n - 1)]
    unset = _deferral_plan(n, 1.0, prices, None, spans, runs)
    zero = _deferral_plan(n, 1.0, prices, 0.0, spans, runs)
    assert unset.total_cost == zero.total_cost
    np.testing.assert_array_equal(
        unset.adequacy_loads[0].power_kw, zero.adequacy_loads[0].power_kw
    )


def test_a_negative_deferral_threshold_is_refused_and_zero_accepted():
    kw = {
        "name": "hws",
        "max_power_kw": 1.0,
        "target_kwh": 2.0,
        "earliest_period": 0,
        "deadline_period": 3,
        "shortfall_price": 10.0,
    }
    with pytest.raises(ValueError, match="must be >= 0"):
        AdequacyLoadConfig(**kw, min_deferral_saving_dollars=-1.0)
    assert AdequacyLoadConfig(**kw, min_deferral_saving_dollars=0.0)


@SETTINGS
@given(st.integers(min_value=2, max_value=12), st.integers(min_value=0, max_value=5))
def test_an_adequacy_window_past_the_plan_is_refused_naming_the_plan(n, past):
    """A window whose deadline lies past the plan is refused before solving,
    and the message names the window's deadline and the plan's range."""
    deadline = n + past
    window = (
        AdequacyWindow(earliest_period=0, deadline_period=deadline, target_kwh=0.5),
    )
    with pytest.raises(ValueError) as refused:
        _pool_plan(n, 0.5, 1.0, np.full(n, 0.3), 0.0, windows=window)
    msg = str(refused.value)
    assert f"({deadline})" in msg and f"(0..{n - 1})" in msg


@SETTINGS
@given(plain_sites(), _num(-1.0, 1.0))
def test_a_period0_pin_holds_the_battery_at_its_rate(site, share):
    """The period-0 pin (#1417, behind #1654's direction hysteresis): a pinned
    net rate is what period 0 does, discharging for a positive pin and
    charging for a negative one, within what the battery and the site can
    take that period."""
    b = site["battery"]
    h0 = float(site["hours"][0])
    if share >= 0:
        room = min(
            b["max_discharge_kw"],
            max(0.0, b["initial_soc_kwh"] - b["min_soc_kwh"])
            * b["discharge_efficiency"]
            / h0,
            float(site["load"][0]) + site["export_limit"],
        )
    else:
        room = min(
            b["max_charge_kw"],
            max(0.0, b["max_soc_kwh"] - b["initial_soc_kwh"])
            / (b["charge_efficiency"] * h0),
            site["import_limit"] - float(site["load"][0]),
        )
    pin = round(share * max(0.0, room) * 0.99, 4)
    plan = _solve(site, period0_pin_net_kw=pin)
    assert plan.status == "optimal"
    assert abs(plan.battery_discharge_kw[0] - max(pin, 0.0)) < 1e-5
    assert abs(plan.battery_charge_kw[0] - max(-pin, 0.0)) < 1e-5


@SETTINGS
@given(plain_sites())
def test_the_grid_flex_signals_agree_with_the_lp(site):
    """The grid flex signals (#491, opt-in `compute_signals`): each kWh headroom is its kW headroom x
    the period's hours, and each forced cost is the LP's own reduced cost on
    that period's import or export, per hour."""
    plan = _solve(site, plan_options={"compute_signals": True})
    sig = plan.grid_signals
    if sig is None:  # HiGHS ranging not valid for this solve (e.g. a MILP)
        return
    h = site["hours"]
    np.testing.assert_allclose(
        sig.grid_import_headroom_kwh, sig.grid_import_headroom_kw * h, atol=1e-9
    )
    np.testing.assert_allclose(
        sig.grid_export_headroom_kwh, sig.grid_export_headroom_kw * h, atol=1e-9
    )
    for t in range(len(h)):
        rc_in = plan.reduced_costs.get(f"grid_import_{t}", 0.0)
        rc_out = plan.reduced_costs.get(f"grid_export_{t}", 0.0)
        assert abs(sig.forced_import_cost[t] * h[t] - rc_in) < 1e-9, t
        assert abs(sig.forced_export_cost[t] * h[t] - rc_out) < 1e-9, t


@SETTINGS
@given(plain_sites())
def test_export_comes_only_from_solar_and_the_battery(site):
    """No power bought from the grid is sold back to it: in every period the
    export is at most the solar used plus the battery's discharge."""
    plan = _solve(site)
    assert (
        plan.grid_export_kw <= plan.solar_used_kw + plan.battery_discharge_kw + 1e-6
    ).all()


@SETTINGS
@given(plain_sites())
def test_with_no_export_import_reaches_its_own_limit_before_the_valve(site):
    """With export shut (limit 0) and the load above the import limit, import
    within the limit is used to the full before the release valve: the
    combined import + export cap must never squeeze import below its own
    limit."""
    site["export_limit"] = 0.0
    site["import_limit"] = round(float(site["load"].max()) * 0.5, 4)
    plan = _solve(site, available=False)
    assert plan.status == "optimal"
    within = plan.grid_import_kw - plan.grid_import_excess_kw
    for t in np.flatnonzero(plan.grid_import_excess_kw > 1e-6):
        assert within[t] >= site["import_limit"] - 1e-5, t


@SETTINGS
@given(sites())
def test_a_run_once_load_runs_in_one_unbroken_block(site):
    """Adequacy loads are run-once by default (#616, `adequacy_semi_continuous`):
    the load is on in one unbroken run of periods, at full power, never split
    into two runs however the prices fall."""
    if site["adequacy"] is None:
        return
    plan = _solve_with_adequacy(site)
    assert plan.status == "optimal"
    on = np.flatnonzero(plan.adequacy_loads[0].power_kw > 1e-6)
    if len(on):
        assert on[-1] - on[0] + 1 == len(on), on


def _run_once_plan(
    price, power, target, *, windows=None, allowed=None, semi=True, value=None
):
    n = len(price)
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(price, dtype=float),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=windows,
                allowed=allowed,
                value_per_kwh=value,
            )
        ],
        adequacy_semi_continuous=semi,
    )


def _runs(on):
    """The (start, end) of each unbroken run of True."""
    out, start = [], None
    for t, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = t
        elif not v and start is not None:
            out.append((start, t - 1))
            start = None
    return out


@SETTINGS
@given(
    st.lists(st.sampled_from([0.05, 0.06, 0.20, 0.30]), min_size=12, max_size=12),
    _num(0.3, 2.0),
    st.integers(1, 6),
    st.sampled_from(["one", "two windows", "gapped mask"]),
)
def test_a_run_once_load_is_full_or_off_in_one_run_per_window(
    price, power, runs, shape
):
    """#616 (Mark Purcell: a 0.65 kW SG Ready heat pump planned as 0.072,
    0.384, 0.65, 0.65, 0.65, 0.403 kW, a shape no on/off contact can
    deliver). Every period is exactly off or exactly full power; each window
    gets one unbroken run and, with a shortfall at $10/kWh, all of its own
    target; and an `allowed` mask with a gap in it is one stretch of
    eligibility for the run, so the load never runs in both, even when its
    target is longer than either stretch."""
    windows = allowed = None
    if shape == "two windows":
        runs = min(runs, 5)
    target = power * runs
    if shape == "two windows":
        windows = (
            AdequacyWindow(earliest_period=0, deadline_period=4, target_kwh=target),
            AdequacyWindow(earliest_period=6, deadline_period=11, target_kwh=target),
        )
    elif shape == "gapped mask":
        allowed = np.zeros(12, dtype=bool)
        allowed[1:5] = True
        allowed[7:11] = True
    plan = _run_once_plan(price, power, target, windows=windows, allowed=allowed)
    assert plan.status == "optimal"
    p_kw = plan.adequacy_loads[0].power_kw
    assert np.all(np.isclose(p_kw, 0.0, atol=1e-6) | np.isclose(p_kw, power, atol=1e-6))
    on = p_kw > 1e-6
    if shape == "two windows":
        assert len(_runs(on[0:5])) <= 1 and len(_runs(on[6:12])) <= 1
        assert int(on[0:5].sum()) == runs and int(on[6:12].sum()) == runs
    else:
        assert len(_runs(on)) <= 1
    if shape == "gapped mask":
        assert not (on[1:5].any() and on[7:11].any())


def test_run_once_off_is_the_continuous_plan():
    """#616: `adequacy_semi_continuous=False` gives back the continuous
    relaxation: a value-priced load fills the cheapest period at full power
    and takes only what is left of its target from the next."""
    plan = _run_once_plan([0.03, 0.07, 0.15], 5.0, 6.0, semi=False, value=0.10)
    assert plan.status == "optimal"
    np.testing.assert_allclose(
        plan.adequacy_loads[0].power_kw[:2], [5.0, 1.0], atol=1e-3
    )


def test_two_run_once_loads_on_a_long_plan_solve_quickly():
    """#616's MIP at a realistic size: 200 half-hour periods and two
    run-once loads solve in well under ten seconds."""
    import time

    n = 200
    started = time.monotonic()
    plan = build_plan(
        periods=PeriodGrid(np.full(n, 0.5)),
        grid=GridConfig(
            import_price=np.random.default_rng(0).uniform(0.05, 0.35, n),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name=name,
                max_power_kw=kw,
                target_kwh=kwh,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
            for name, kw, kwh in (("hws", 0.65, 2.0), ("pool", 1.1, 4.0))
        ],
    )
    assert plan.status == "optimal"
    assert time.monotonic() - started < 10.0


@SETTINGS
@given(
    _num(0.3, 3.0),
    _num(1.0, 4.0),
    st.integers(1, 4),
    _num(0.5, 30.0),
    st.integers(1, 6),
    st.booleans(),
)
def test_a_reachable_target_is_met_at_any_price_below_its_shortfall_price(
    price, power, length, target, runs, semi
):
    """Mark Purcell's audit, item 5 (hard service constraints), with #477's
    soft shortfall. Hot water by its deadline must not be traded away when
    power gets merely dear: at any import price under the shortfall price
    (here 1x to 10x a 30c tariff, against the $10 default), a target the
    load can reach in its window is met in full. One it cannot reach no
    longer takes the plan down: it solves, runs flat out, and reports exactly
    the unreachable part as shortfall. Run once (#616), the target is whole
    periods (a fractional one is the cheaper of rounding up and a priced
    shortfall, `::test_each_window_delivers_its_own_target_inside_its_own_periods`)."""
    if semi:
        target = power * runs
    n = length + 2
    reach = power * length
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.full(n, 0.05),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=1,
                deadline_period=length,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ],
        adequacy_semi_continuous=semi,
    )
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    if target <= reach - 1e-6:
        assert al.delivered_by_deadline_kwh >= target - 1e-5
        assert al.shortfall_kwh == pytest.approx(0.0, abs=1e-5)
    else:
        assert al.delivered_by_deadline_kwh == pytest.approx(reach, abs=1e-5)
        assert al.shortfall_kwh == pytest.approx(target - reach, abs=1e-5)


@SETTINGS
@given(
    st.lists(st.sampled_from([0.03, 0.07, 0.15, 0.30]), min_size=3, max_size=8),
    _num(0.5, 5.0),
    st.data(),
)
def test_a_continuous_load_is_bought_in_its_cheapest_periods(prices, power, data):
    """#606 and #616 off: a load with a target and room to spare runs its
    cheapest periods first, at full power, and takes only what is left of
    its target from the next; it pays exactly that greedy cheapest cost and
    never runs past its target."""
    n = len(prices)
    target = data.draw(_num(0.1, power * n))
    plan = _run_once_plan(prices, power, target, semi=False)
    assert plan.status == "optimal"
    p_kw = plan.adequacy_loads[0].power_kw
    assert float(np.sum(p_kw)) == pytest.approx(target, abs=1e-5)
    need, least = target, 0.0
    for price in sorted(prices):
        take = min(power, need)
        least += price * take
        need -= take
    assert float(np.sum(np.asarray(prices) * p_kw)) == pytest.approx(least, abs=1e-5)


@pytest.mark.parametrize("width", [0.25, 0.5, 1.0])
def test_a_run_once_load_stays_in_one_block_even_when_splitting_is_cheaper(width):
    """Two cheap pairs of periods with dear ones between: one run of four
    periods has to cross the dear ones, two runs would not. A run-once load
    (#616) still runs in one unbroken block."""
    price = np.array([0.05, 0.05, 1.0, 1.0, 0.05, 0.05, 1.0, 1.0])
    n = len(price)
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=price,
            export_price=np.zeros(n),
            import_limit_kw=10.0,
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
                name="pool",
                max_power_kw=2.0,
                target_kwh=2.0 * width * 4,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=20.0,
            )
        ],
    )
    assert plan.status == "optimal"
    pool = plan.adequacy_loads[0]
    on = np.flatnonzero(pool.power_kw > 1e-6)
    assert len(on) == 4 and on[-1] - on[0] == 3, on


def _thermal_plan(n, width, thermal, price=None):
    return build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.5) if price is None else price,
            export_price=np.zeros(n),
            import_limit_kw=20.0,
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
        thermal_loads=[ThermalLoadConfig(**thermal)],
    )


@SETTINGS
@given(
    st.integers(min_value=2, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(30.0, 60.0),
    _num(0.01, 0.2),
    _num(0.5, 15.0),
)
def test_a_tank_in_warmer_air_does_not_cool(n, width, start, coeff, warmer):
    """Ambient coupling (#481): the loss is the gap to the outdoor air, and a
    heating load is never credited with warmth from a hot day, so a tank
    already at its target, in air warmer than it, holds its temperature
    without heating."""
    plan = _thermal_plan(
        n,
        width,
        {
            "name": "hws",
            "max_power_kw": 3.0,
            "initial_temperature_c": start,
            "target_temperature_c": start,
            "earliest_period": 0,
            "deadline_period": n - 1,
            "heating_rate_c_per_kwh": 5.0,
            "idle_decay_c_per_hour": 1.0,
            "loss_coeff_per_h": coeff,
            "ambient_c": tuple([start + warmer] * n),
        },
    )
    assert plan.status == "optimal"
    (tp,) = plan.thermal_loads
    assert (tp.power_kw <= TOL).all()
    np.testing.assert_allclose(tp.temperature_c, np.full(n, start), atol=1e-6)


@SETTINGS
@given(
    st.integers(min_value=2, max_value=8),
    st.sampled_from([0.25, 0.5]),
    _num(1.0, 3.0),
)
def test_an_unreachable_thermal_target_still_heats_flat_out(n, width, power):
    """A thermal target the load can't reach by its deadline (#774's
    fallback): the guarantee is relaxed, and each degree it falls short is
    priced at the adequacy shortfall price per kWh of heating. At $7.50/kWh,
    under that $10, heating is still worth it, so the load runs at full power
    up to the deadline."""
    rate, start = 2.0, 20.0
    reach = start + rate * power * width * n
    price = np.full(n, 7.5)
    plan = _thermal_plan(
        n,
        width,
        {
            "name": "hws",
            "max_power_kw": power,
            "initial_temperature_c": start,
            "target_temperature_c": min(round(reach + 10.0, 4), 98.0),
            "earliest_period": 0,
            "deadline_period": n - 1,
            "heating_rate_c_per_kwh": rate,
            "idle_decay_c_per_hour": 0.0,
        },
        price=price,
    )
    assert plan.status == "optimal"
    assert plan.thermal_guarantee_relaxed == ["hws"]
    np.testing.assert_allclose(plan.thermal_loads[0].power_kw, power, atol=1e-5)


@pytest.mark.parametrize("width", [0.25, 0.5, 1.0])
def test_a_comfort_floor_is_worth_heating_to_when_it_costs_more(width):
    """A comfort floor (`comfort_floor_c`, never above the target): each
    degree-period under it costs `comfort_floor_cost`. Heating now instead of
    at the deadline costs 0.75 of that per degree more, so a tank under the
    floor is heated to the floor now rather than later."""
    rate, start, floor, cost = 5.0, 40.0, 45.0, 1.0
    later = 0.5
    price = np.array([later + 0.75 * cost * rate, later])
    power = (floor - start) / (rate * width)
    plan = _thermal_plan(
        2,
        width,
        {
            "name": "hws",
            "max_power_kw": power * 2.0,
            "initial_temperature_c": start,
            "target_temperature_c": floor,
            "earliest_period": 0,
            "deadline_period": 1,
            "heating_rate_c_per_kwh": rate,
            "idle_decay_c_per_hour": 0.0,
            "comfort_floor_c": floor,
            "comfort_floor_cost": cost,
        },
        price=price,
    )
    assert plan.status == "optimal"
    assert abs(plan.thermal_loads[0].temperature_c[0] - floor) < 1e-5


@SETTINGS
@given(
    plain_sites(),
    st.lists(_num(0.0, 0.5), min_size=24, max_size=24),
    _num(0.5, 10.0),
)
def test_the_export_bonus_is_claimed_on_real_export_within_its_cap(
    site, premiums, volume
):
    """The two-tier export bonus (P2P): a premium on top of the export price
    for up to `export_bonus_volume_kwh`. The bonus is only ever claimed on
    export that happened, the volume claimed stays within the cap, and the
    premium the plan collects is what the bonus's own settlement
    (`realized_export_bonus_credit`) pays for that export, the dearest
    periods first, and the plan's cost counts the premium exactly once."""
    n = len(site["hours"])
    bonus_price = np.array(premiums[:n])
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
            export_bonus_price=bonus_price,
            export_bonus_volume_kwh=volume,
        ),
        batteries=[BatteryConfig(**site["battery"])],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    h = site["hours"]
    bonus = plan.export_bonus_kw
    assert (bonus >= -TOL).all()
    assert (bonus <= plan.grid_export_kw + 1e-6).all()
    assert float(np.sum(bonus * h)) <= volume + 1e-6
    collected = float(np.sum(bonus_price * bonus * h))
    grid = GridConfig(
        import_price=site["import_price"],
        export_price=site["export_price"],
        import_limit_kw=site["import_limit"],
        export_limit_kw=site["export_limit"],
        export_bonus_price=bonus_price,
        export_bonus_volume_kwh=volume,
    )
    settled = realized_export_bonus_credit(
        grid_export_kw=plan.grid_export_kw, hours=h, grid=grid, period_starts=None
    )
    assert abs(collected - settled) < 1e-5
    # The objective counts the premium once: the plan costs its flows less the
    # premium collected, up to the bonus's latest-preferred tie-break (a few
    # hundredths of a cent at most).
    site = {**site, "sheddable": None}
    assert abs(plan.total_cost - (_parts(site, plan) - collected)) < 1e-3


@SETTINGS
@given(
    plain_sites(),
    st.integers(min_value=0, max_value=23),
    _num(0.5, 6.0),
    st.one_of(st.none(), _num(0.0, 6.0)),
)
def test_a_committed_export_window_shuts_import_and_caps_export_at_its_band(
    site, at, kw, extra
):
    """A P2P fixed export window (2026-08-20, household finding): no import
    within the limit in a committed period, so the commitment can't be met by
    buying power to sell back, and export never above the commitment, or
    above `fixed_export_max_kw` where a band is set."""
    n = len(site["hours"])
    window = list(range(at % n, min(at % n + 2, n)))
    fixed = np.full(n, np.nan)
    band = None if extra is None else np.full(n, np.nan)
    for t in window:
        fixed[t] = kw
        if band is not None:
            band[t] = round(kw + extra, 4)
    site["export_limit"] = max(site["export_limit"], kw + (extra or 0.0) + 1.0)
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
            fixed_export_kw=fixed,
            fixed_export_max_kw=band,
        ),
        batteries=[BatteryConfig(**site["battery"])],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    within = plan.grid_import_kw - plan.grid_import_excess_kw
    for t in window:
        assert within[t] <= TOL, t
        cap = kw if band is None else max(kw, float(band[t]))
        assert plan.grid_export_kw[t] <= cap + 1e-6, t


@SETTINGS
@given(
    st.integers(min_value=1, max_value=24),
    st.data(),
    st.one_of(st.none(), st.just(0.0)),
)
def test_the_export_bonus_settlement_pays_nothing_without_a_bonus_to_pay(
    n, data, volume
):
    """The bonus's settlement (`realized_export_bonus_credit`) pays nothing
    when no bonus is configured, or when its volume is zero, whatever was
    exported and whatever the premium."""
    export = np.array(data.draw(st.lists(_num(0.0, 10.0), min_size=n, max_size=n)))
    hours = np.array(data.draw(st.lists(_num(0.25, 1.0), min_size=n, max_size=n)))
    premium = np.array(data.draw(st.lists(_num(0.0, 0.5), min_size=n, max_size=n)))
    grid = GridConfig(
        import_price=np.full(n, 0.3),
        export_price=np.full(n, 0.05),
        import_limit_kw=10.0,
        export_limit_kw=10.0,
        export_bonus_price=None if volume is None else premium,
        export_bonus_volume_kwh=volume,
    )
    assert (
        realized_export_bonus_credit(
            grid_export_kw=export, hours=hours, grid=grid, period_starts=None
        )
        == 0.0
    )


@SETTINGS
@given(sites(), st.integers(min_value=1, max_value=3))
def test_a_zero_export_pin_leaves_import_open(site, span):
    """Only a positive pin is a P2P commitment (#1761). The hours after the
    block are pinned to 0 kW export, and there the inverter still imports
    when the battery can't carry the house: within the limit, never through
    the release valve. The battery starts at its floor and the sun is down,
    so the first period's load can only come from the grid."""
    site = {**site, "adequacy": None, "thermal": None, "sheddable": None}
    n = len(site["hours"])
    site["solar"] = site["solar"].copy()
    site["solar"][0] = 0.0
    site["load"] = site["load"].copy()
    site["load"][0] = max(float(site["load"][0]), 0.5)
    site["import_limit"] = max(site["import_limit"], float(site["load"][0]) + 1.0)
    battery = {**site["battery"], "initial_soc_kwh": site["battery"]["min_soc_kwh"]}
    fixed = np.full(n, np.nan)
    fixed[: min(span, n)] = 0.0
    plan = build_plan(
        periods=PeriodGrid(site["hours"]),
        grid=GridConfig(
            import_price=site["import_price"],
            export_price=site["export_price"],
            import_limit_kw=site["import_limit"],
            export_limit_kw=site["export_limit"],
            fixed_export_kw=fixed,
        ),
        batteries=[BatteryConfig(**battery)],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    assert plan.grid_import_excess_kw[0] <= TOL
    within = plan.grid_import_kw[0] - plan.grid_import_excess_kw[0]
    assert within >= float(site["load"][0]) - 1e-4


@SETTINGS
@given(
    plain_sites(),
    st.lists(_num(0.0, 0.5), min_size=24, max_size=24),
    _num(0.2, 5.0),
    st.data(),
)
def test_the_export_bonus_cap_resets_at_each_calendar_midnight(
    site, premiums, volume, data
):
    """The bonus volume is a per-day allowance: on a plan that crosses
    midnight, each calendar day's claimed volume stays within the cap on its
    own, and the plan collects what the settlement pays when it groups the
    same periods by day."""
    n = len(site["hours"])
    width = float(site["hours"][0])
    before = data.draw(st.integers(min_value=1, max_value=n - 1))
    start = datetime(2026, 10, 20, tzinfo=UTC) - timedelta(hours=width * before)
    periods = PeriodGrid(site["hours"], start=start)
    bonus_price = np.array(premiums[:n])
    grid = GridConfig(
        import_price=site["import_price"],
        export_price=site["export_price"],
        import_limit_kw=site["import_limit"],
        export_limit_kw=site["export_limit"],
        export_bonus_price=bonus_price,
        export_bonus_volume_kwh=volume,
    )
    plan = build_plan(
        periods=periods,
        grid=grid,
        batteries=[BatteryConfig(**site["battery"])],
        solar=SolarConfig(forecast_kw=site["solar"]),
        loads=[LoadConfig(name="house", forecast_kw=site["load"])],
    )
    assert plan.status == "optimal"
    h = site["hours"]
    bonus = plan.export_bonus_kw
    assert (bonus <= plan.grid_export_kw + 1e-6).all()
    for day in (range(before), range(before, n)):
        assert float(sum(bonus[t] * h[t] for t in day)) <= volume + 1e-6
    collected = float(np.sum(bonus_price * bonus * h))
    settled = realized_export_bonus_credit(
        grid_export_kw=plan.grid_export_kw,
        hours=h,
        grid=grid,
        period_starts=periods.period_starts,
    )
    assert abs(collected - settled) < 1e-5


# --- #1818: an unreachable departure target is a priced shortfall --------------

_DEP_N, _DEP_H, _EV_KW = 12, 0.5, 12.0
_DEP_EFF = 0.95**0.5


def _ev(deadline, target, initial=3.0, **extra):
    """#1818's EV: 60 kWh, 5% SoC below a 20% floor, 12 kW."""
    return BatteryConfig(
        name="Test EV",
        capacity_kwh=60.0,
        initial_soc_kwh=initial,
        min_soc_kwh=12.0,
        max_soc_kwh=60.0,
        max_charge_kw=_EV_KW,
        max_discharge_kw=_EV_KW,
        charge_efficiency=_DEP_EFF,
        discharge_efficiency=_DEP_EFF,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.0,
        must_have_soc_by_period_index=deadline,
        must_have_soc_kwh=target,
        **extra,
    )


def _reach(periods, kw=_EV_KW, initial=3.0):
    return min(initial + periods * kw * _DEP_EFF * _DEP_H, 60.0)


def _departure_plan(ev, *, options=None, adequacy=None):
    lp._lex_calibration_failed_until = 0.0  # module state: never skip phases
    return build_plan(
        periods=PeriodGrid(
            np.full(_DEP_N, _DEP_H), start=datetime(2026, 10, 16, 18, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(_DEP_N, 0.30),
            export_price=np.full(_DEP_N, 0.05),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=4.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.05,
            ),
            ev,
        ],
        solar=SolarConfig(forecast_kw=np.zeros(_DEP_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(_DEP_N))],
        adequacy_loads=adequacy,
        solve_options=options,
    )


def _ev_of(plan):
    return next(b for b in plan.batteries if b.name == "Test EV")


@pytest.mark.parametrize("binaries", [False, True])
@pytest.mark.parametrize("options", [None, "calibrated"])
def test_an_unreachable_departure_target_charges_flat_out_and_prices_the_rest(
    options, binaries, caplog
):
    """#1818 (a test install, 17 Oct 2026: an EV at 5% with 80% due in under
    four hours of charging made the hard row unsatisfiable, presolve rejected
    the model, and every cycle published `infeasible`, no dispatch for any
    battery). Now the plan solves on every path production takes (the
    calibrated solve, with binaries in the model) and logs no #773 ERROR:
    the EV charges flat out to the deadline, the gap is the shortfall, and
    it is priced as SoC penalty at the soft-SoC rate (#781: not left to
    land in the terminal credit)."""
    network._DEPARTURE_UNREACHABLE_WARNED.clear()
    adequacy = (
        [
            AdequacyLoadConfig(
                name=f"load{i}",
                max_power_kw=2.0,
                target_kwh=3.0,
                deadline_period=_DEP_N - 1,
                earliest_period=0,
                shortfall_price=50.0,
            )
            for i in range(3)
        ]
        if binaries
        else None
    )
    with caplog.at_level(logging.ERROR, logger="solver.lp"):
        plan = _departure_plan(
            _ev(1, 48.0),
            options=lp.CalibratedOptions() if options else None,
            adequacy=adequacy,
        )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert plan.status == "optimal"
    ev = _ev_of(plan)
    np.testing.assert_allclose(ev.charge_kw[:2], [_EV_KW, _EV_KW], atol=1e-6)
    assert ev.soc_kwh[1] == pytest.approx(_reach(2), abs=1e-5)
    short = plan.departure_target_shortfall_kwh["Test EV"]
    assert short == pytest.approx(48.0 - _reach(2), abs=1e-5)
    if not binaries:
        rate = network.DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER * 0.30
        under = sum(max(0.0, 12.0 - float(s)) for s in ev.soc_kwh) + sum(
            max(0.0, 4.0 - float(s)) for s in plan.batteries[0].soc_kwh
        )
        assert plan.soc_penalty_cost == pytest.approx(rate * (under + short), abs=1e-4)


def test_a_departure_due_now_while_away_is_all_shortfall():
    """#1818, from 08:00: the deadline resolved to period 0 with the car
    away, so nothing can charge; the whole target is short and it solves."""
    ev = _ev(0, 48.0, available=False, unavailable_until_period_index=2)
    plan = _departure_plan(ev, options=lp.CalibratedOptions())
    assert plan.status == "optimal"
    assert plan.departure_target_shortfall_kwh["Test EV"] == pytest.approx(45.0)


def test_a_shared_charger_ceiling_limits_the_reach():
    """#1818 with #467: a 12 kW car on a 7 kW shared charger reaches less
    than its own rate suggests; a target between the two reaches is
    unreachable, so it is priced, not left to make the plan infeasible."""
    target = 0.5 * (_reach(2, 7.0) + _reach(2))
    ev = _ev(1, target, shared_charger_group="garage", shared_charger_max_kw=7.0)
    plan = _departure_plan(ev, options=lp.CalibratedOptions())
    assert plan.status == "optimal"
    assert "Test EV" in plan.departure_target_shortfall_kwh


@pytest.mark.parametrize(
    ("deadline", "target", "initial"),
    [(10, 48.0, 3.0), (1, None, 3.0), (6, 40.0, 30.0)],
    ids=["below-floor-recovers", "exactly-at-reach", "in-range"],
)
def test_a_reachable_target_keeps_the_hard_row(deadline, target, initial):
    """#1818: whenever the target is reachable, the model is the one it
    was (the hard `battery_departure_deadline_*` row, no soft row, no
    shortfall) and the target is met, including exactly at reach."""
    target = _reach(2) if target is None else target
    plan = _departure_plan(
        _ev(deadline, target, initial), options=lp.CalibratedOptions()
    )
    assert plan.status == "optimal"
    assert plan.departure_target_shortfall_kwh == {}
    assert _ev_of(plan).soc_kwh[deadline] >= target - 1e-5
    rows = set(plan.duals or {})
    assert "battery_departure_deadline_Test EV" in rows
    assert "battery_departure_deadline_soft_Test EV" not in rows


def test_an_unreachable_departure_warns_once():
    """#1818: one WARNING per departure, then DEBUG on every later cycle."""
    network._DEPARTURE_UNREACHABLE_WARNED.clear()
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("solver.network")
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.DEBUG)
    try:
        _departure_plan(_ev(1, 48.0))
        _departure_plan(_ev(1, 48.0))
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    levels = [r.levelno for r in records if "cannot reach" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


# --- Mark Purcell's audit item 2: every objective term moves a decision ----------


def _ablation_battery(**over):
    return BatteryConfig(
        **{
            "name": "battery",
            "capacity_kwh": 20.0,
            "initial_soc_kwh": 10.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 20.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": 0.99,
            "discharge_efficiency": 0.99,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            "headroom_value": 0.0,
            **over,
        }
    )


def _empty_site(n, export, battery, **grid):
    """No load, no sun, import too dear to ever buy: the battery's only
    choice is how much of its energy to sell, so its terminal terms decide."""
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 5.0),
            export_price=np.full(n, export) if np.isscalar(export) else export,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            **grid,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.zeros(n))],
    )


@SETTINGS
@given(_num(0.0, 0.3), _num(0.0, 0.3), _num(0.0, 0.3), st.integers(2, 6))
def test_the_terminal_values_decide_what_is_kept(export, salvage, headroom, n):
    """Mark Purcell's solver audit, item 2 (objective completeness): a term
    that never changes an outcome is dead weight. With nothing else to
    trade, a stored kWh either stays (worth `salvage_value`) or is sold
    (worth the export price, less the discharge cost, plus
    `headroom_value` for the room it frees). Whichever is worth more by a
    clear margin decides the final SoC, at a corner: kept at the starting
    10 kWh, or sold down to Min SoC."""
    keep = salvage
    sell = export * 0.99 - 0.01 + headroom
    if abs(keep - sell) < 0.02:
        return
    plan = _empty_site(
        n, export, _ablation_battery(salvage_value=salvage, headroom_value=headroom)
    )
    assert plan.status == "optimal"
    final = float(plan.battery_soc_kwh[-1])
    assert final == pytest.approx(10.0 if keep > sell else 2.0, abs=1e-4)


@SETTINGS
@given(
    st.lists(st.booleans(), min_size=2, max_size=6),
    _num(0.05, 0.6),
    _num(1.0, 30.0),
    _num(3.0, 18.0),
)
def test_an_export_bonus_buys_exports_the_base_price_never_would(
    in_window, bonus, volume, stored
):
    """Audit item 2, the two-tier P2P bonus: at a base export price under
    the discharge cost nothing is sold without it; with it, the battery
    sells into the bonus periods exactly what it can, up to the cap: the
    least of the cap, its energy above Min SoC as delivered, and what the
    export limit carries in those periods."""
    n = len(in_window)
    price = np.where(in_window, bonus, 0.0)
    battery = _ablation_battery(
        initial_soc_kwh=2.0 + stored, max_soc_kwh=22.0, capacity_kwh=22.0
    )
    off = _empty_site(n, 0.005, battery)
    on = _empty_site(
        n,
        0.005,
        battery,
        export_bonus_price=price,
        export_bonus_volume_kwh=volume,
    )
    assert off.status == on.status == "optimal"
    assert float(np.sum(off.grid_export_kw)) < 1e-6
    carried = 10.0 * sum(in_window)  # max discharge, at the meter
    claimed = float(np.sum(on.export_bonus_kw))
    assert claimed == pytest.approx(min(volume, stored * 0.99, carried), abs=1e-4)


@SETTINGS
@given(
    _num(1.5, 6.0),
    _num(0.05, 0.6),
    _num(0.01, 1.0),
    st.integers(1, 4),
    st.sampled_from([5 / 60, 0.5, 1.0]),
)
def test_the_shed_cost_decides_what_is_shed_beyond_the_limit(
    forecast, price, shed_cost, n, width
):
    """Audit item 2, a sheddable load's `shed_cost`: against a 1 kW import
    limit and no battery to help, the part above the limit must be shed;
    the rest is shed only when shedding costs less than buying it, both per
    kWh whatever the period length."""
    if abs(shed_cost - price) < 1e-3:
        return
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.zeros(n),
            import_limit_kw=1.0,
            export_limit_kw=5.0,
        ),
        batteries=[_ablation_battery(available=False)],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        sheddable_loads=[
            SheddableLoadConfig(
                name="pool",
                forecast_kw=np.full(n, forecast),
                shed_cost=shed_cost,
                min_fraction=0.0,
            )
        ],
    )
    assert plan.status == "optimal"
    want = forecast if shed_cost < price else forecast - 1.0
    np.testing.assert_allclose(plan.sheddable_loads[0].shed_kw, want, atol=1e-5)


# --- #467 item 4: a trip deadline is bought in the cheapest hours before it -----


def _trip_plan(prices, deadline, target):
    n = len(prices)
    ev = BatteryConfig(
        name="ev",
        capacity_kwh=60.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=6.0,
        max_soc_kwh=60.0,
        max_charge_kw=7.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.0,
        salvage_value=0.0,
        must_have_soc_by_period_index=None if target is None else deadline,
        must_have_soc_kwh=target,
    )
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(prices, dtype=float),
            export_price=np.full(n, 0.03),
            import_limit_kw=15.0,
            export_limit_kw=15.0,
        ),
        batteries=[ev],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(n, 0.5))],
    )


@SETTINGS
@given(
    st.lists(st.sampled_from([0.05, 0.12, 0.30, 0.45]), min_size=4, max_size=16),
    st.data(),
)
def test_a_trip_deadline_is_bought_in_the_cheapest_hours_before_it(prices, data):
    """#467 item 4 (Mark Purcell named HAEO's `scenario7_ev`: cheap-window
    pre-charge, trip delivered by the deadline). HAEO's #361 shipped a
    calendar schema nothing was wired to; here the deadline the calendar
    resolves to is what makes the car charge. It holds the target at the
    deadline, buys the energy at the least it can cost (the cheapest hours
    up to and including the deadline, filled at the charger's 7 kW), and
    charges nothing after. Without the deadline it charges nothing at all,
    so it is the deadline doing it, not the price curve."""
    n = len(prices)
    deadline = data.draw(st.integers(0, n - 1))
    reach = 10.0 + 7.0 * 0.95 * (deadline + 1)
    target = data.draw(_num(10.5, min(60.0, reach)))
    plan = _trip_plan(prices, deadline, target)
    assert plan.status == "optimal"
    (ev,) = plan.batteries
    assert float(ev.soc_kwh[deadline]) >= target - 1e-5
    np.testing.assert_allclose(ev.charge_kw[deadline + 1 :], 0.0, atol=1e-6)
    need = (target - 10.0) / 0.95
    least = 0.0
    for price in sorted(prices[: deadline + 1]):
        take = min(7.0, need)
        least += price * take
        need -= take
    paid = float(np.sum(np.asarray(prices) * ev.charge_kw))
    assert paid == pytest.approx(least, abs=1e-5)
    idle = _trip_plan(prices, deadline, None)
    np.testing.assert_allclose(idle.batteries[0].charge_kw, 0.0, atol=1e-6)


@SETTINGS
@given(
    st.integers(1, 3),
    _num(4.0, 15.0),
    st.integers(1, 4),
    _num(0.5, 3.0),
    _num(0.0, 1.0),
)
def test_a_block_is_delivered_net_and_the_shortfall_is_bought_after_it(
    block_h, rate, after_h, house, share
):
    """#1610 with #1761: 0.94.444's plan for the reference household. A
    P2P block is a NET export commitment, so no ordinary import inside it,
    and the self-consume hours after it are pinned to 0 kW export, which
    leaves import open there. A battery holding enough for the block but
    not the block and the night after must deliver the block exactly and
    buy the rest after it as ordinary import, never as penalised import
    inside the block (the $64 of it that plan booked)."""
    n = block_h + after_h
    eff = 0.96
    spare = share * house * after_h / eff
    initial = 2.0 + (rate + house) * block_h / eff + spare
    fixed = np.array([rate] * block_h + [0.0] * after_h)
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.23),
            export_price=np.full(n, 0.14),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=80.0,
                initial_soc_kwh=initial,
                min_soc_kwh=2.0,
                max_soc_kwh=80.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=eff,
                discharge_efficiency=eff,
                charge_cost=0.0,
                discharge_cost=0.01,
                salvage_value=0.05,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, house))],
    )
    assert plan.status == "optimal"
    inside, after = slice(0, block_h), slice(block_h, n)
    np.testing.assert_allclose(plan.grid_export_kw[inside], rate, atol=1e-6)
    np.testing.assert_allclose(plan.grid_import_kw[inside], 0.0, atol=1e-6)
    np.testing.assert_allclose(plan.grid_import_excess_kw, 0.0, atol=1e-6)
    np.testing.assert_allclose(plan.grid_export_kw[after], 0.0, atol=1e-6)
    bought = float(np.sum(plan.grid_import_kw[after]))
    assert bought == pytest.approx(house * after_h - spare * eff, abs=1e-4)


@SETTINGS
@given(
    st.lists(_num(1.0, 14.0), min_size=1, max_size=3),
    _num(0.3, 3.0),
    st.booleans(),
)
def test_a_block_is_net_with_the_house_on_top_when_import_is_cheap(rates, house, short):
    """The household's rule (7 Oct 2026): a P2P block's rate is net to the
    grid, "12 kW to the grid, the house on top". Measured that evening: with
    import at 18.4c in the block and dearer after, the plan bought the house
    from the grid and still counted the full export (meter -10.6 kW for a
    12 kW block). Now nothing is imported in the block and the battery
    carries the rate plus the house. A battery that can carry the block but
    not the house as well delivers the block and shows the rest as a breach
    through the penalised valve (#390), not as a quiet shortfall in net
    export."""
    k = len(rates)
    n = k + 5
    eff = 0.9263
    need = sum(rates) / eff
    initial = 2.444 + (need + 0.5 * house * k / eff if short else 60.0 + need)
    fixed = np.full(n, np.nan)
    fixed[:k] = rates
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.array([0.184] * k + [0.40] * 5),
            export_price=np.full(n, 0.09),
            import_limit_kw=44.0,
            export_limit_kw=44.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=122.2,
                initial_soc_kwh=initial,
                min_soc_kwh=2.444,
                max_soc_kwh=122.2,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=eff,
                discharge_efficiency=eff,
                charge_cost=0.005,
                discharge_cost=0.005,
                salvage_value=0.30,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, house))],
    )
    assert plan.status == "optimal"
    within = plan.grid_import_kw - plan.grid_import_excess_kw
    np.testing.assert_allclose(plan.grid_export_kw[:k], rates, atol=1e-6)
    np.testing.assert_allclose(within[:k], 0.0, atol=1e-6)
    if short:
        assert plan.import_cap_breach_kwh > 1e-3
    else:
        np.testing.assert_allclose(plan.grid_import_kw[:k], 0.0, atol=1e-6)
        net = plan.battery_discharge_kw[:k] - plan.battery_charge_kw[:k]
        np.testing.assert_allclose(net, np.asarray(rates) + house, atol=1e-3)


# --- #328 / #781: soft SoC bounds recover, and their penalty is what it says -----


@SETTINGS
@given(
    st.booleans(),
    _num(0.0, 0.95),
    _num(0.3, 6.0),
    st.sampled_from([0.5, 1.0]),
    st.booleans(),
    st.sampled_from([2.5, 50.0]),
)
def test_a_battery_outside_its_bounds_recovers_flat_out(
    below, depth, rate_kw, width, tiers, penalty
):
    """#328 (Mark Purcell), #1987: Min and Max SoC are soft, so a live reading
    outside them solves rather than raising, and the plan comes back
    inside as fast as the battery can: from under the floor it charges
    flat out until it reaches it, from over the ceiling it discharges flat
    out until it is under. The penalty dominates every price, so it is never
    worth staying out; and it is never inflated either, with terminal-value
    tiers in play or not (the segment-fill row was infeasible below the
    floor, #328, and above the ceiling, #1987). The plan reports exactly the rate x every kWh outside
    (#781: it used to be misread as a terminal credit)."""
    n, lo, hi = 12, 8.0, 32.0
    start = lo * depth if below else hi + (40.0 - hi) * (1.0 - depth)
    breakpoints = [(6.0, 0.3), (12.0, 0.15), (6.0, 0.05)] if tiers else None
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=40.0,
        initial_soc_kwh=start,
        min_soc_kwh=lo,
        max_soc_kwh=hi,
        max_charge_kw=rate_kw,
        max_discharge_kw=rate_kw,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.15,
        terminal_value_breakpoints=breakpoints,
        terminal_value_period_indices=[n - 1] if tiers else None,
    )
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.25),
            export_price=np.full(n, 0.10),
            import_limit_kw=44.0,
            export_limit_kw=44.0,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
        soft_soc_penalty_per_kwh=penalty,
    )
    assert plan.status == "optimal"
    soc = plan.battery_soc_kwh
    step = np.cumsum(np.full(n, rate_kw * width))
    if below:
        np.testing.assert_array_less(np.minimum(lo, start + step * 0.975) - 1e-5, soc)
    else:
        np.testing.assert_array_less(soc, np.maximum(hi, start - step / 0.975) + 1e-5)
    outside = np.clip(lo - soc, 0.0, None).sum() + np.clip(soc - hi, 0.0, None).sum()
    assert plan.soc_penalty_cost == pytest.approx(penalty * outside, abs=1e-6)
