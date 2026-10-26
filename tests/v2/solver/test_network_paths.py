"""Layer 1 of the rebuilt suite (#1837): `network.py` paths the 21 Oct fair
score found only the old tests checking (seed 1837, old 22/30, new 20/30).

Each states what the LP is for, with its own oracle: a plan imports up to
its own limit, a thermal load with no weather forecast is the flat model,
the spike override lifts a committed export only in period 0, and the
earliness tie-breaks (adequacy and thermal) cost nothing at the plan's
first instant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import network
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SharedCircuitConfig,
    SheddableLoadConfig,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import BatteryPlan, Plan, build_plan
from test_plan_invariants import SETTINGS, _num, sites


def _idle_battery(**over):
    """A battery that can't help: unavailable, so the grid carries the load."""
    return BatteryConfig(
        **{
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
            "available": False,
            **over,
        }
    )


@SETTINGS
@given(
    st.integers(min_value=2, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(1.0, 30.0),
    _num(0.0, 0.95),
)
def test_a_plan_imports_up_to_its_own_import_limit(n, width, load, export_share):
    """With the load exactly at the import limit and a smaller export limit,
    the grid carries the whole load: no release-valve import, no penalty.
    The import-plus-export row only stops both directions at once."""
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 0.05),
            import_limit_kw=load,
            export_limit_kw=load * export_share,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load))],
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_import_kw, load, atol=1e-6)
    assert plan.grid_import_excess_penalty_cost <= 1e-9


@SETTINGS
@given(sites(), _num(0.01, 0.2))
def test_a_loss_coefficient_without_weather_is_the_flat_model(site, coeff):
    """Ambient coupling needs both halves (#481): a loss coefficient with an
    empty outdoor forecast plans exactly as the flat model does, and setting
    only one half is refused."""
    th = site["thermal"]
    if th is None:
        return

    def plan_with(**coupling):
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
            thermal_loads=[ThermalLoadConfig(**th, **coupling)],
        )

    flat = plan_with()
    assert flat.status == "optimal"
    # An empty forecast passes validation: the coefficient alone must not
    # switch the decay model on.
    other = plan_with(loss_coeff_per_h=coeff, ambient_c=())
    assert other.status == "optimal"
    np.testing.assert_allclose(
        other.thermal_loads[0].temperature_c,
        flat.thermal_loads[0].temperature_c,
        atol=1e-6,
    )
    assert abs(other.total_cost - flat.total_cost) < 1e-6
    # Half-set is refused outright rather than half-applied.
    for coupling in (
        {"loss_coeff_per_h": coeff, "ambient_c": None},
        {"loss_coeff_per_h": None, "ambient_c": (10.0,)},
    ):
        with pytest.raises(ValueError, match="both be set"):
            plan_with(**coupling)


@SETTINGS
@given(
    st.integers(min_value=2, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(1.0, 20.0),
    _num(0.0, 0.95),
)
def test_a_plan_exports_up_to_its_own_export_limit(n, width, limit, import_share):
    """The mirror of the import case: with spare solar well beyond the export
    limit, a paying export price and a smaller import limit, the plan exports
    exactly its limit. The import-plus-export row must not cut either side."""
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 0.1),
            import_limit_kw=limit * import_share,
            export_limit_kw=limit,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.full(n, limit + 5.0)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_export_kw, limit, atol=1e-6)


@SETTINGS
@given(
    st.sampled_from([0.25, 0.5]),
    _num(1.0, 4.0),
    _num(1.0, 8.0),
    _num(0.05, 0.5),
)
def test_a_thermal_load_on_a_price_tie_heats_at_the_first_instant(
    width, power, rate, price
):
    """The thermal earliness tie-break charges each kWh by how far into the
    plan it runs, nothing at the first instant: with two equally priced
    periods and heat enough for one, all of it goes in period 0."""
    n = 2
    start = 40.0
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.zeros(n),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        thermal_loads=[
            ThermalLoadConfig(
                name="hws",
                max_power_kw=power,
                initial_temperature_c=start,
                target_temperature_c=start + rate * power * width,
                earliest_period=0,
                deadline_period=1,
                heating_rate_c_per_kwh=rate,
                idle_decay_c_per_hour=0.0,
            )
        ],
    )
    assert plan.status == "optimal"
    pw = plan.thermal_loads[0].power_kw
    assert pw[1] <= 1e-6
    assert abs(pw[0] - power) <= 1e-6


@SETTINGS
@given(
    st.integers(min_value=2, max_value=8),
    st.data(),
    _num(10.0, 30.0),
    _num(0.0, 4.0),
)
def test_a_per_period_export_envelope_caps_each_period_and_curtails_the_rest(
    n, data, solar_kw, load_kw
):
    """#493 (Signals 4/7 of #489): a DNSP's dynamic export envelope is a
    per-period `export_limit_kw`. With a paying export price, spare sun and
    an idle battery, each period exports min(surplus, its own envelope),
    curtails exactly what the envelope refuses, and the envelope's shadow
    price (#491's `forced_export_cost`) is non-zero exactly where it
    binds."""
    surplus = solar_kw - load_kw
    envelope = np.array(
        data.draw(st.lists(_num(0.5, 40.0), min_size=n, max_size=n)), dtype=float
    )
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.10),
            import_limit_kw=50.0,
            export_limit_kw=envelope,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.full(n, solar_kw)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load_kw))],
        compute_signals=True,
    )
    assert plan.status == "optimal"
    want = np.minimum(surplus, envelope)
    np.testing.assert_allclose(plan.grid_export_kw, want, atol=1e-6)
    np.testing.assert_allclose(
        plan.solar_curtailed_kw, np.maximum(surplus - envelope, 0.0), atol=1e-6
    )
    binding = envelope < surplus - 1e-6
    forced = plan.grid_signals.forced_export_cost
    for t in range(n):
        if binding[t]:
            assert forced[t] != 0.0, t
        elif envelope[t] > surplus + 1e-6:
            assert forced[t] == 0.0, t


@SETTINGS
@given(st.integers(min_value=2, max_value=6), _num(5.0, 40.0), _num(5.0, 40.0))
def test_a_uniform_envelope_is_the_scalar_plan(n, imp, exp):
    """#493: an envelope that is the same every period is the plain scalar
    limit, to the cost."""

    def plan(limit_imp, limit_exp):
        return build_plan(
            periods=PeriodGrid(np.ones(n)),
            grid=GridConfig(
                import_price=np.full(n, 0.30),
                export_price=np.full(n, 0.05),
                import_limit_kw=limit_imp,
                export_limit_kw=limit_exp,
            ),
            batteries=[_idle_battery(available=True)],
            solar=SolarConfig(forecast_kw=np.full(n, 10.0)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 3.0))],
        )

    scalar, array = plan(imp, exp), plan(np.full(n, imp), np.full(n, exp))
    assert scalar.status == array.status == "optimal"
    np.testing.assert_allclose(scalar.grid_export_kw, array.grid_export_kw, atol=1e-9)
    np.testing.assert_allclose(scalar.grid_import_kw, array.grid_import_kw, atol=1e-9)
    assert scalar.total_cost == pytest.approx(array.total_cost, abs=1e-9)


@SETTINGS
@given(st.integers(min_value=2, max_value=6), st.data(), _num(4.0, 12.0))
def test_an_import_envelope_below_the_load_uses_the_excess_valve(n, data, load_kw):
    """#493 with #390: a per-period import envelope below the load is
    met through the import-excess valve in exactly those periods, by exactly
    the shortfall; the plan stays optimal."""
    envelope = np.array(
        data.draw(st.lists(_num(0.5, 20.0), min_size=n, max_size=n)), dtype=float
    )
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=envelope,
            export_limit_kw=50.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load_kw))],
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(
        plan.grid_import_excess_kw, np.maximum(load_kw - envelope, 0.0), atol=1e-6
    )
    assert plan.import_cap_breach_kwh == pytest.approx(
        float(np.maximum(load_kw - envelope, 0.0).sum()), abs=1e-6
    )


def _adequacy_tie_plan(
    n,
    *,
    price=None,
    allowed=None,
    windows=None,
    budget=None,
    target=1.0,
    power=1.0,
    earliest=0,
    deadline=None,
):
    kwargs = {} if budget is None else {"adequacy_earliness_budget_kw": budget}
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.20) if price is None else price,
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
                earliest_period=earliest,
                deadline_period=n - 1 if deadline is None else deadline,
                allowed=allowed,
                windows=windows,
                shortfall_price=50.0,
            )
        ],
        **kwargs,
    )


@pytest.mark.parametrize("n", [24, 96])
def test_the_earliness_spread_is_bounded_by_its_budget(n):
    """#613: forcing the same kWh into the last period instead of the first
    costs at most the budget per kWh over any horizon (so it can never pass
    for a real price difference), is clearly active (over half the budget
    at the horizon's far end), and is nothing with the budget at zero."""
    budget = network.DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW

    def forced(t, b=None):
        allowed = np.zeros(n, dtype=bool)
        allowed[t] = True
        return _adequacy_tie_plan(n, allowed=allowed, budget=b).total_cost

    spread = forced(n - 1) - forced(0)
    assert 0.5 * budget < spread <= budget + 1e-9
    assert forced(n - 1, 0.0) == pytest.approx(forced(0, 0.0), abs=1e-9)


def test_a_real_cheaper_later_price_beats_earliness():
    """#613: materiality. A real late price dip, far larger than the budget,
    still decides where the load runs."""
    n = 24
    price = np.full(n, 0.30)
    price[20] = 0.02
    power = _adequacy_tie_plan(n, price=price).adequacy_loads[0].power_kw
    assert power[20] == pytest.approx(1.0, abs=1e-3)
    assert power[:20].sum() == pytest.approx(0.0, abs=1e-3)


def _circuit_plan(n, members, cap, *, prices=None, circuits="cap"):
    """Adequacy loads `members` = [(name, power, target)], each free in
    every period, optionally on one shared circuit of `cap` kW."""
    shared = (
        [
            SharedCircuitConfig(
                name="hws_circuit",
                member_names=tuple(m[0] for m in members),
                max_combined_power_kw=cap,
            )
        ]
        if circuits == "cap"
        else circuits
    )
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.linspace(0.01, 1.0, n) if prices is None else prices,
            export_price=np.zeros(n),
            import_limit_kw=40.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name=name,
                max_power_kw=power,
                target_kwh=target,
                earliest_period=0,
                deadline_period=n - 1,
                shortfall_price=50.0,
            )
            for name, power, target in members
        ],
        shared_circuits=shared,
    )


@SETTINGS
@given(
    st.integers(min_value=1, max_value=4),
    st.lists(_num(1.0, 4.0), min_size=2, max_size=3),
    _num(0.3, 1.0),
)
def test_a_shared_circuit_never_carries_more_than_its_cap(n, powers, share):
    """A shared circuit (two HWS heaters that must never draw at once): the
    members' combined power never exceeds the cap in any period, though the
    price would have them all run in the cheapest one. Each member wants one
    full-power period; where the cap leaves room to sequence them every
    target is met, and otherwise the plan stays optimal (#477) with the
    shortfall exactly what the cap cannot fit."""
    members = [(f"hws{i}", p, p) for i, p in enumerate(powers)]
    cap = max(powers) * (1.0 + share)
    plan = _circuit_plan(n, members, cap)
    assert plan.status == "optimal"
    power = np.array([al.power_kw for al in plan.adequacy_loads])
    assert (power.sum(axis=0) <= cap + 1e-6).all()
    need = sum(powers)
    delivered = sum(al.delivered_by_deadline_kwh for al in plan.adequacy_loads)
    short = sum(al.shortfall_kwh for al in plan.adequacy_loads)
    assert delivered + short == pytest.approx(need, abs=1e-4)
    # Without the cap, the cheap first period takes them all at once.
    if need > cap + 1e-6:
        free = _circuit_plan(n, members, cap, circuits=None)
        assert sum(al.power_kw[0] for al in free.adequacy_loads) > cap + 1e-6
    if n * cap >= need + 1e-9 and all(p <= cap for p in powers):
        # Room to sequence: a simple greedy fit across n periods suffices
        # when every member is one period's full power.
        bins, fits = [cap] * n, True
        for p in sorted(powers, reverse=True):
            spot = next((i for i, b in enumerate(bins) if b >= p - 1e-9), None)
            if spot is None:
                fits = False
                break
            bins[spot] -= p
        if fits:
            assert short == pytest.approx(0.0, abs=1e-4)


def test_one_shared_period_too_small_for_both_is_a_shortfall():
    """#477: two 3.7 kW heaters on a 3.7 kW circuit with one period between
    them: 3.7 kWh is delivered and 3.7 kWh is short, not an infeasible plan."""
    plan = _circuit_plan(1, [("hws_l1", 3.7, 3.7), ("hws_l3", 3.7, 3.7)], 3.7)
    assert plan.status == "optimal"
    assert sum(al.delivered_by_deadline_kwh for al in plan.adequacy_loads) == (
        pytest.approx(3.7, abs=1e-3)
    )
    assert sum(al.shortfall_kwh for al in plan.adequacy_loads) == pytest.approx(
        3.7, abs=1e-3
    )


def test_a_circuit_naming_an_unknown_load_is_refused_by_name():
    """A circuit member that is not a configured adequacy load is a caller
    mistake: refused, naming the missing load. No circuits at all, None or
    an empty list, is the same plan."""
    circuit = [
        SharedCircuitConfig(
            name="c", member_names=("hws_l1", "hws_l3"), max_combined_power_kw=3.7
        )
    ]
    with pytest.raises(ValueError, match="hws_l3"):
        _circuit_plan(2, [("hws_l1", 3.7, 1.0)], 3.7, circuits=circuit)
    prices = np.array([0.05, 0.30])
    a = _circuit_plan(2, [("hws_l1", 3.7, 2.0)], 3.7, prices=prices, circuits=None)
    b = _circuit_plan(2, [("hws_l1", 3.7, 2.0)], 3.7, prices=prices, circuits=[])
    np.testing.assert_array_equal(
        a.adequacy_loads[0].power_kw, b.adequacy_loads[0].power_kw
    )
    assert a.total_cost == b.total_cost


def _tank_plan(n=24, **coupling):
    th = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 40.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": n - 1,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.3,
        **coupling,
    }
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.ones(n))],
        thermal_loads=[ThermalLoadConfig(**th)],
    )


def _tank_kwh(plan) -> float:
    return float(plan.thermal_loads[0].power_kw.sum())


@SETTINGS
@given(_num(-5.0, 35.0), _num(0.0, 20.0), _num(0.005, 0.05), _num(0.0, 0.04))
def test_a_colder_day_or_a_leakier_tank_never_needs_less_heat(
    ambient, colder_by, coeff, leakier_by
):
    """#481: with ambient coupling the outdoor forecast reaches the plan.
    Holding the 60 C deadline target (met exactly, the #774 guarantee
    unrelaxed), a colder day or a larger loss coefficient never schedules
    fewer kWh, and the difference is real once the tank sits above both
    ambients.

    Coefficients up to 0.09/h (a time constant over 11 h; a hot-water tank
    is nearer 0.01-0.03/h). Above about 0.1/h a leakier tank plans LESS
    heat: the coast line puts the largest decay steps first, and #897's
    idle-fraction scaling cancels decay in exactly those full-power
    periods (CUTOVER.md, found while checking)."""
    n = 24
    base = _tank_plan(n, loss_coeff_per_h=coeff, ambient_c=(ambient,) * n)
    cold = _tank_plan(n, loss_coeff_per_h=coeff, ambient_c=(ambient - colder_by,) * n)
    leaky = _tank_plan(n, loss_coeff_per_h=coeff + leakier_by, ambient_c=(ambient,) * n)
    for plan in (base, cold, leaky):
        assert plan.status == "optimal"
        assert plan.thermal_guarantee_relaxed == []
        assert plan.thermal_loads[0].temperature_c[-1] == pytest.approx(60.0, abs=1e-5)
    assert _tank_kwh(cold) >= _tank_kwh(base) - 1e-6
    assert _tank_kwh(leaky) >= _tank_kwh(base) - 1e-6
    if colder_by > 1.0:
        assert _tank_kwh(cold) > _tank_kwh(base) + 1e-6


def test_the_ambient_gap_is_clamped_and_the_coast_line_errs_low():
    """#481: a tank below ambient gains no free warmth (no cooling term
    exists), so it needs exactly the rise over the heating rate; and the
    coast reference under-estimates decay for a heated tank: less heat than
    a flat rate pinned to the coast line's own first step."""
    n = 24
    hot = _tank_plan(n, loss_coeff_per_h=0.05, ambient_c=(80.0,) * n)
    assert _tank_kwh(hot) == pytest.approx((60.0 - 40.0) / 8.0, abs=1e-4)
    ambient = _tank_plan(n, loss_coeff_per_h=0.05, ambient_c=(0.0,) * n)
    flat = _tank_plan(n, idle_decay_c_per_hour=0.05 * 1.0 * (40.0 - 0.0))
    assert _tank_kwh(ambient) < _tank_kwh(flat)


def test_a_short_outdoor_forecast_holds_its_last_value():
    """#481: an outdoor series shorter than the plan holds its last value
    (sample-and-hold, like every other series) rather than reverting to the
    flat model mid-horizon."""
    n = 24
    short = _tank_plan(n, loss_coeff_per_h=0.05, ambient_c=(0.0, 0.0, 0.0))
    full = _tank_plan(n, loss_coeff_per_h=0.05, ambient_c=(0.0,) * n)
    np.testing.assert_allclose(
        short.thermal_loads[0].temperature_c,
        full.thermal_loads[0].temperature_c,
        atol=1e-6,
    )


# --- #492: per-load intent bands -----------------------------------------------


def _signal_plan(import_price, *, sheddable=None, adequacy=None, signals=True):
    n = len(import_price)
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(import_price, dtype=float),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        sheddable_loads=sheddable,
        adequacy_loads=adequacy,
        adequacy_semi_continuous=False,
        compute_signals=signals,
    )


def test_load_signals_are_opt_in():
    """#492: without `compute_signals` the plan carries no load signals."""
    plan = _signal_plan(
        [0.2, 0.2],
        sheddable=[
            SheddableLoadConfig(name="pool", forecast_kw=np.ones(2), shed_cost=0.2)
        ],
        signals=False,
    )
    assert plan.status == "optimal" and plan.load_signals == []


# --- the export floor ("don't sell below", household 20 Oct 2026) ---------------

_FLOOR_N = 12
_FLOOR_H = np.full(_FLOOR_N, 0.5)


def _floor_battery(soc, capacity=15.0, **over):
    return BatteryConfig(
        **{
            "name": "home",
            "capacity_kwh": capacity,
            "initial_soc_kwh": soc,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": capacity,
            "max_charge_kw": 36.0,
            "max_discharge_kw": 36.0,
            "charge_efficiency": 0.97,
            "discharge_efficiency": 0.97,
            "charge_cost": 0.0,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            **over,
        }
    )


def _floor_plan(floor, *, soc, surplus, morning_export, fixed=None, capacity=15.0, **b):
    """The 20 Oct morning: four sunny half-hours with `surplus` kW spare,
    two of cheaper grid with no sun, then an evening at 40c."""
    n = _FLOOR_N
    exp = np.array([morning_export] * 4 + [0.06] * 2 + [0.30] * 6)
    imp = np.array([0.16] * 4 + [0.084] * 2 + [0.40] * 6)
    return build_plan(
        periods=PeriodGrid(_FLOOR_H),
        grid=GridConfig(
            import_price=imp,
            export_price=exp,
            import_limit_kw=60.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
            export_floor_price=floor,
        ),
        batteries=[_floor_battery(soc, capacity, **b)],
        solar=SolarConfig(forecast_kw=np.array([3.0 + surplus] * 4 + [0.0] * 8)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 3.0))],
    )


@SETTINGS
@given(_num(0.02, 0.14), _num(1.0, 6.0), _num(2.0, 15.0))
def test_below_the_floor_spare_solar_is_stored_while_there_is_room(
    morning_export, surplus, soc
):
    """The household's threshold, not an override. With the morning's
    export price below a 15c floor, spare solar goes into the battery
    before any of it is sold, and only what the battery cannot take is
    exported; nothing is curtailed (the residual value keeps a full battery
    exporting). Without the floor, the control: the same site is free to
    sell. The penalty is exactly the floor's rates on what was exported."""
    plan = _floor_plan(0.15, soc=soc, surplus=surplus, morning_export=morning_export)
    assert plan.status == "optimal"
    sunny = slice(0, 4)
    stored = float(np.sum(plan.batteries[0].charge_kw[sunny] * _FLOOR_H[sunny]))
    sold = float(np.sum(plan.grid_export_kw[sunny] * _FLOOR_H[sunny]))
    spare = surplus * 2.0
    room = (15.0 - soc) / 0.97
    assert stored == pytest.approx(min(spare, room), abs=1e-4)
    assert sold == pytest.approx(max(0.0, spare - room), abs=1e-4)
    assert float(np.sum(plan.solar_curtailed_kw)) < 1e-6
    rates = network.export_floor_penalty_rates(plan.effective_export_price, 0.15)
    assert plan.export_floor_penalty_cost == pytest.approx(
        float(np.sum(rates * plan.grid_export_kw * _FLOOR_H)), rel=1e-9, abs=1e-12
    )


def test_without_the_floor_the_morning_solar_is_sold():
    """The 20 Oct control: 9c now against 8.4c grid later, so on price
    alone the plan sells the spare solar and refills from the grid."""
    plan = _floor_plan(0.0, soc=5.0, surplus=5.0, morning_export=0.09)
    assert plan.status == "optimal"
    assert float(np.sum(plan.grid_export_kw[:4] * _FLOOR_H[:4])) > 9.0
    assert float(np.sum(plan.grid_import_kw[4:6] * _FLOOR_H[4:6])) > 9.0


@SETTINGS
@given(_num(0.02, 0.3), _num(1.0, 6.0), _num(2.0, 15.0), st.booleans())
def test_a_floor_never_reached_changes_nothing(morning_export, surplus, soc, off):
    """Off (0) is byte-identical to no floor at all. A floor at or below
    every export price the plan sees decides nothing either: the plan is
    the price-only plan, with no penalty, the evening export included."""
    base = _floor_plan(0.0, soc=soc, surplus=surplus, morning_export=morning_export)
    floor = 0.0 if off else min(morning_export, 0.06)
    floored = _floor_plan(
        floor, soc=soc, surplus=surplus, morning_export=morning_export
    )
    assert floored.export_floor_penalty_cost == 0.0
    if off:
        np.testing.assert_array_equal(floored.grid_export_kw, base.grid_export_kw)
        np.testing.assert_array_equal(
            floored.batteries[0].soc_kwh, base.batteries[0].soc_kwh
        )
        assert floored.total_cost == base.total_cost
    else:
        np.testing.assert_allclose(
            floored.grid_export_kw, base.grid_export_kw, atol=1e-6
        )
        np.testing.assert_allclose(
            floored.batteries[0].soc_kwh, base.batteries[0].soc_kwh, atol=1e-6
        )


def test_a_spike_override_keeps_the_floor_on_the_free_part():
    """Mark Purcell, #1924: at period 0 under a spike override the P2P
    commitment is a floor, not a pin, and export above it is free. The
    export floor stays live on that free part; on later committed periods
    it does not."""
    fixed = np.full(_FLOOR_N, np.nan)
    fixed[0] = 2.0
    fixed[6:9] = 2.0
    exp = np.array([0.10] + [0.09] * 3 + [0.06] * 2 + [0.10] * 3 + [0.30] * 3)
    plan = build_plan(
        periods=PeriodGrid(_FLOOR_H),
        grid=GridConfig(
            import_price=np.array([0.16] * 4 + [0.084] * 2 + [0.40] * 6),
            export_price=exp,
            import_limit_kw=60.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
            export_floor_price=0.15,
        ),
        batteries=[_floor_battery(15.0, spike_override_discharge_kw=10.0)],
        solar=SolarConfig(forecast_kw=np.zeros(_FLOOR_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(_FLOOR_N, 3.0))],
    )
    assert plan.status == "optimal"
    assert float(plan.grid_export_kw[0]) > 2.0 + 1e-6
    rates = network.export_floor_penalty_rates(plan.effective_export_price, 0.15)
    expected = float(rates[0] * plan.grid_export_kw[0] * _FLOOR_H[0])
    assert expected > 0
    assert plan.export_floor_penalty_cost == pytest.approx(expected, rel=1e-6)


def test_a_negative_floor_is_refused():
    with pytest.raises(ValueError, match="export_floor_price"):
        GridConfig(
            import_price=np.zeros(1),
            export_price=np.zeros(1),
            import_limit_kw=1.0,
            export_limit_kw=1.0,
            export_floor_price=-0.01,
        )


# --- #467: the per-period away mask ----------------------------------------------

_SWING_N = 10


def _swing_plan(*batteries, n=_SWING_N, load=1.0):
    """Dirt cheap for the first half, dear for the second: every reason to
    charge early and discharge late, so a period at exactly 0 is gated, not
    merely unattractive."""
    half = n // 2
    price = np.array([0.05] * half + [0.60] * (n - half))
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=price,
            export_price=price - 0.02,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=list(batteries),
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load))],
    )


def _car(name="ev", **over):
    return BatteryConfig(
        **{
            "name": name,
            "capacity_kwh": 40.0,
            "initial_soc_kwh": 20.0,
            "min_soc_kwh": 4.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.005,
            "discharge_cost": 0.01,
            "salvage_value": 0.10,
            **over,
        }
    )


def _moves(b, periods):
    return sum(float(b.charge_kw[t]) + float(b.discharge_kw[t]) for t in periods)


@SETTINGS
@given(st.frozensets(st.integers(0, 14)), st.integers(0, 9))
def test_an_away_mask_gates_its_own_periods_and_no_others(away, extra):
    """#467 (Mark Purcell, 13 Sep: "Build an internal gating gap please"):
    two trips in a day, which a prefix gate cannot say. A masked period is
    exactly idle; an index past this solve's horizon is ignored, not an
    error (a calendar day's mask on a shorter solve); and the home periods
    stay usable: with a cheap home period before a dear one the battery
    still trades, and masking one more period never makes the plan
    cheaper."""
    n = _SWING_N
    plan = _swing_plan(_car(unavailable_period_indices=away))
    assert plan.status == "optimal"
    (ev,) = plan.batteries
    inside = {t for t in away if t < n}
    for t in inside:
        assert ev.charge_kw[t] == 0.0 and ev.discharge_kw[t] == 0.0, t
    clipped = _swing_plan(_car(unavailable_period_indices=frozenset(inside)))
    np.testing.assert_array_equal(clipped.batteries[0].charge_kw, ev.charge_kw)
    np.testing.assert_array_equal(clipped.batteries[0].discharge_kw, ev.discharge_kw)
    home = [t for t in range(n) if t not in inside]
    if any(t < n // 2 for t in home) and any(t >= n // 2 for t in home):
        assert _moves(ev, home) > 1.0
    more = _swing_plan(_car(unavailable_period_indices=frozenset(inside | {extra})))
    assert more.total_cost >= plan.total_cost - 1e-6


def test_no_mask_is_the_unmasked_plan_exactly():
    """#467: None (the default) and an empty mask add nothing at all."""
    base = _swing_plan(_car()).batteries[0]
    for mask in (None, frozenset()):
        other = _swing_plan(_car(unavailable_period_indices=mask)).batteries[0]
        np.testing.assert_array_equal(other.charge_kw, base.charge_kw)
        np.testing.assert_array_equal(other.discharge_kw, base.discharge_kw)


@SETTINGS
@given(st.frozensets(st.integers(0, _SWING_N - 1), min_size=1))
def test_an_away_mask_is_one_battery_s_alone(away):
    """#467: one car out driving never gates the home pack beside it."""
    plan = _swing_plan(
        _car("home"), _car("ev", unavailable_period_indices=away), load=2.0
    )
    assert plan.status == "optimal"
    by = {b.name: b for b in plan.batteries}
    assert _moves(by["ev"], away) == 0.0
    assert _moves(by["home"], range(_SWING_N)) > 1.0


def test_an_away_mask_is_normalised_and_a_negative_index_refused():
    """#467: a list or `np.flatnonzero(~is_home)` (np.int64, as the writer
    feeds it) becomes a frozenset of plain ints, so the frozen config stays
    hashable; a negative index is refused."""
    cfg = _car(unavailable_period_indices=[3, 1, 1, 2])
    assert cfg.unavailable_period_indices == frozenset({1, 2, 3})
    assert isinstance(hash(cfg), int)
    cfg = _car(unavailable_period_indices=np.flatnonzero(np.array([0, 1, 1, 0])))
    assert cfg.unavailable_period_indices == frozenset({1, 2})
    assert all(type(i) is int for i in cfg.unavailable_period_indices)
    with pytest.raises(ValueError, match="non-negative"):
        _car(unavailable_period_indices=frozenset({-1, 2}))


# --- #491: the grid operator's headroom signals --------------------------------


def _operator_plan(n, solar, load, import_cap, export_cap, signals=True):
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.10),
            import_limit_kw=import_cap,
            export_limit_kw=export_cap,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.full(n, solar)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load))],
        compute_signals=signals,
    )


@SETTINGS
@given(st.integers(1, 4), _num(0.0, 60.0), _num(0.5, 20.0), _num(2.0, 40.0))
def test_the_export_headroom_is_the_surplus_the_cap_holds_back(
    n, solar, load, export_cap
):
    """#491 (Signals 2/7 of #489). With spare sun at or over the export cap,
    the export headroom is exactly what the cap holds back (0 at exact
    saturation, 35 kW for 45 kW of surplus on a 10 kW cap): how far an
    operator could lift it before curtailment stops, or before the
    connection's own combined cap (#266, import + export within the larger
    limit) binds instead. Where the cap binds,
    relaxing it would earn the export price, so its forced cost is minus
    that price (a reduced cost at an upper bound, minimising). The down-flex
    figure mirrors it (until #493's envelope)."""
    surplus = solar - load
    if surplus < export_cap:
        return
    plan = _operator_plan(n, solar, load, 50.0, export_cap)
    assert plan.status == "optimal"
    sig = plan.grid_signals
    np.testing.assert_allclose(
        sig.grid_export_headroom_kw,
        min(surplus, max(50.0, export_cap)) - export_cap,
        atol=1e-6,
    )
    np.testing.assert_allclose(sig.flex_available_down_kw, sig.grid_export_headroom_kw)
    if surplus > export_cap + 1e-3:
        np.testing.assert_allclose(sig.forced_export_cost, -0.10, atol=1e-6)


@SETTINGS
@given(
    st.integers(1, 4),
    st.one_of(st.just(0.0), _num(0.0, 10.0)),
    _num(0.5, 20.0),
    _num(0.5, 40.0),
)
def test_a_deficit_has_the_import_cap_above_it_and_zero_below_it(n, solar, extra, room):
    """#492's switchboard headroom on the same balance row: with only the
    grid to serve a deficit, the load could grow by the import cap's room
    and shrink by the deficit itself before the period's price changes.
    With no sun, the grid is the only source: the import has no headroom of
    its own and no forced cost (an equality holds it, not a bound). With
    sun it could replace solar, so that holds only without it."""
    load = solar + extra
    plan = _operator_plan(n, solar, load, extra + room, 50.0)
    assert plan.status == "optimal"
    sig = plan.grid_signals
    np.testing.assert_allclose(plan.grid_import_kw, extra, atol=1e-6)
    np.testing.assert_allclose(sig.load_headroom_up_kwh, room, atol=1e-6)
    np.testing.assert_allclose(sig.load_headroom_down_kwh, extra, atol=1e-6)
    if solar == 0.0:
        np.testing.assert_allclose(sig.grid_import_headroom_kw, 0.0, atol=1e-6)
        np.testing.assert_allclose(sig.forced_import_cost, 0.0, atol=1e-6)


def test_the_operator_signals_are_opt_in():
    """#491: ranging is opt-in (it took a 288-period solve from 0.6 s to
    5.6 s). Without it there are no grid signals and no battery ranging;
    the battery's physical headroom needs none and is always there."""
    plan = _operator_plan(4, 0.0, 5.0, 50.0, 50.0, signals=False)
    assert plan.status == "optimal" and plan.grid_signals is None
    (bs,) = plan.battery_signals
    assert bs.available_up_kw is not None and bs.available_down_kw is not None
    assert bs.available_up_ranging_kw is None and bs.available_down_ranging_kw is None


# --- #567: the price spike override pins period 0, the first battery only --------


def _spike_battery(name="home", **over):
    return BatteryConfig(
        **{
            "name": name,
            "capacity_kwh": 40.0,
            "initial_soc_kwh": 30.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.0,
            **over,
        }
    )


def _spike_plan(batteries, price, n=4, fixed=None):
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.full(n, price / 10.0),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
            fixed_export_kw=fixed,
        ),
        batteries=batteries,
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
    )


@SETTINGS
@given(st.integers(2, 6), _num(0.0, 10.0), st.booleans())
def test_a_spike_override_pins_period_0_and_nothing_else(n, rate, second):
    """#567 ("sell into a price spike, deliberately, right now"). At a
    price too cheap to ever discharge for (import 0.1c, under the 1c
    discharge cost), the first battery discharges exactly the household's
    rate in period 0 and nothing after; what the house does not use is
    exported. A second battery with the same field set is never pinned:
    the override is the first battery's alone."""
    batteries = [_spike_battery(spike_override_discharge_kw=rate)]
    if second:
        batteries.append(_spike_battery("ev", spike_override_discharge_kw=rate))
    plan = _spike_plan(batteries, 0.001, n)
    assert plan.status == "optimal"
    first = plan.batteries[0]
    assert float(first.discharge_kw[0]) == pytest.approx(rate, abs=1e-5)
    assert float(first.charge_kw[0]) == pytest.approx(0.0, abs=1e-6)
    np.testing.assert_allclose(first.discharge_kw[1:], 0.0, atol=1e-6)
    assert float(plan.grid_export_kw[0]) == pytest.approx(
        max(0.0, rate - 2.0), abs=1e-5
    )
    if second:
        np.testing.assert_allclose(plan.batteries[1].discharge_kw, 0.0, atol=1e-6)


def test_a_zero_spike_rate_is_a_pin_and_none_is_not():
    """#567: 0.0 is a deliberate "nothing right now", not "no override".
    At a price that pays to discharge, an unset override lets period 0
    discharge; 0.0 holds it at nothing."""
    free = _spike_plan([_spike_battery()], 0.60)
    held = _spike_plan([_spike_battery(spike_override_discharge_kw=0.0)], 0.60)
    assert free.status == held.status == "optimal"
    assert float(free.batteries[0].discharge_kw[0]) > 1.0
    assert float(held.batteries[0].discharge_kw[0]) == pytest.approx(0.0, abs=1e-6)


@SETTINGS
@given(_num(0.0, 1.5), st.integers(2, 6))
def test_a_spike_rate_caps_period_0_and_frees_the_rest(rate, n):
    """#567: the override is period 0's exact rate in both directions. At a
    price that pays to discharge, period 0 holds to a rate below the house's
    2 kW, and every later period is free to discharge more than it."""
    plan = _spike_plan([_spike_battery(spike_override_discharge_kw=rate)], 0.60, n)
    assert plan.status == "optimal"
    first = plan.batteries[0]
    assert float(first.discharge_kw[0]) == pytest.approx(rate, abs=1e-5)
    assert float(first.discharge_kw[1:].max()) > rate + 0.4


def test_a_spike_rate_below_a_p2p_block_falls_short_and_still_plans():
    """#694's caveat with #1761: export never exceeds what the battery
    and sun supply, so a spike rate below an active P2P block's rate
    cannot meet the block. The block falls short at a price, visibly; the
    plan is published, not lost to an infeasible solve."""
    fixed = np.full(4, np.nan)
    fixed[0] = 11.5
    plan = _spike_plan(
        [_spike_battery(spike_override_discharge_kw=2.0)], 0.001, fixed=fixed
    )
    assert plan.status == "optimal"
    assert float(plan.grid_export_kw[0]) < 11.5 - 1e-6


@pytest.mark.parametrize("rate", [-1.0, 10.000001, 999.0])
def test_a_spike_rate_outside_the_battery_s_range_is_refused(rate):
    with pytest.raises(ValueError):
        _spike_battery(spike_override_discharge_kw=rate)


def test_a_spike_rate_at_the_discharge_limit_is_accepted():
    assert (
        _spike_battery(spike_override_discharge_kw=10.0).spike_override_discharge_kw
        == 10.0
    )


# --- #1654: the soft battery reserve ----------------------------------------------


def _reserve_plan(start, reserve, release, export, window, n=8, imp=5.0, load=0.0):
    battery = BatteryConfig(
        name="home",
        capacity_kwh=100.0,
        initial_soc_kwh=start,
        min_soc_kwh=2.0,
        max_soc_kwh=100.0,
        max_charge_kw=36.0,
        max_discharge_kw=36.0,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.0,
        discharge_cost=0.01,
        salvage_value=0.0,
        reserve_kwh=reserve,
        reserve_release_price=release,
        reserve_period_indices=window,
    )
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, imp),
            export_price=np.asarray(export, dtype=float) * np.ones(n),
            import_limit_kw=60.0,
            export_limit_kw=40.0,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load))],
    )


def test_a_battery_below_the_reserve_is_not_made_to_refill():
    """#1654: held starts at the lesser of the SoC and the reserve, so a
    battery already under it is never charged for energy it never had."""
    for window in (None, frozenset(range(4))):
        plan = _reserve_plan(3.0, 10.0, 0.60, 0.286, window, imp=0.39, load=1.0)
        assert plan.status == "optimal"
        assert float(np.sum(plan.batteries[0].charge_kw)) == pytest.approx(
            0.0, abs=1e-6
        )


def test_opening_a_window_short_costs_the_release_price():
    """Household, 20 Oct 2026: a window ahead opened 7 kWh short of a 15 kWh
    reserve. At a 30c release price, topping up beforehand at 22.8c is
    cheaper than opening short, so it does; at 10c it is not, so it doesn't."""
    window = frozenset(range(6, 12))
    topped = _reserve_plan(8.0, 15.0, 0.30, 0.15, window, n=12, imp=0.228)
    short = _reserve_plan(8.0, 15.0, 0.10, 0.15, window, n=12, imp=0.228)
    assert float(topped.batteries[0].soc_kwh[5]) >= 15.0 - 1e-6
    assert float(short.batteries[0].soc_kwh[5]) < 15.0 - 1.0


# --- price risk aversion (household 21 Aug 2026; split by Mark Purcell) ----------


def _risk_plan(imp, exp, *, upper=None, lower=None, a=0.0, b=0.0):
    n = len(imp)
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(imp, dtype=float),
            export_price=np.asarray(exp, dtype=float),
            import_limit_kw=44.0,
            export_limit_kw=44.0,
            import_price_upper=upper,
            export_price_lower=lower,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=2.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.975,
                discharge_efficiency=0.975,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.15,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
        import_price_risk_aversion=a,
        export_price_risk_aversion=b,
    )


# --- #774: the thermal deadline is hard, relaxed only when it cannot be met --------


def test_no_thermal_loads_is_the_plan_without_them():
    """#774: `thermal_loads=None` adds nothing; omitting it plans the same."""
    n = 6
    kw = {
        "periods": PeriodGrid(np.ones(n)),
        "grid": GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        "batteries": [_idle_battery(available=True)],
        "solar": SolarConfig(forecast_kw=np.zeros(n)),
        "loads": [LoadConfig(name="load", forecast_kw=np.ones(n))],
    }
    omitted = build_plan(**kw)
    given = build_plan(**kw, thermal_loads=None)
    assert given.thermal_loads == [] and given.thermal_guarantee_relaxed == []
    assert given.total_cost == omitted.total_cost
    np.testing.assert_array_equal(given.battery_soc_kwh, omitted.battery_soc_kwh)


# --- #467 stage 1: several batteries, each its own -------------------------------


def _pack(name, eff, **over):
    return BatteryConfig(
        **{
            "name": name,
            "capacity_kwh": 40.0,
            "initial_soc_kwh": 20.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": eff,
            "discharge_efficiency": eff,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.10,
            **over,
        }
    )


@SETTINGS
@given(_num(0.0, 10.0), _num(10.0, 30.0), _num(0.5, 3.0))
def test_the_rate_limit_holds_each_battery_to_its_own_previous_value(
    home_prev, ev_prev, rate
):
    """#467 stage 1, mechanism 2 (the cross-solve rate limit): each battery's
    period-0 discharge stays within the rate of ITS OWN previous value,
    matched by name. The previous plan lists them in the other order, so
    matching by position, or by the old aggregate, lands a battery in the
    other's band, which its own limits cannot always reach."""
    start = datetime(2026, 9, 8, 17, 0, tzinfo=UTC)
    periods = PeriodGrid(np.ones(1), start=start)
    total = home_prev + ev_prev
    previous = Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=np.zeros(1),
        battery_discharge_kw=np.array([total]),
        battery_soc_kwh=np.zeros(1),
        grid_import_kw=np.zeros(1),
        grid_export_kw=np.array([max(0.0, total - 1.0)]),
        export_bonus_kw=np.zeros(1),
        solar_used_kw=np.zeros(1),
        solar_curtailed_kw=np.zeros(1),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        soc_penalty_cost=0.0,
        iterations=0,
        batteries=[
            BatteryPlan(
                name="ev",
                charge_kw=np.zeros(1),
                discharge_kw=np.array([ev_prev]),
                soc_kwh=np.array([80.0]),
            ),
            BatteryPlan(
                name="home",
                charge_kw=np.zeros(1),
                discharge_kw=np.array([home_prev]),
                soc_kwh=np.array([10.0]),
            ),
        ],
    )
    plan = build_plan(
        periods=periods,
        grid=GridConfig(
            import_price=np.full(1, 0.30),
            export_price=np.full(1, 0.05),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        ),
        batteries=[
            _pack(
                "home", 0.95, capacity_kwh=20.0, initial_soc_kwh=15.0, max_soc_kwh=20.0
            ),
            _pack(
                "ev",
                0.95,
                capacity_kwh=100.0,
                initial_soc_kwh=80.0,
                max_soc_kwh=100.0,
                max_discharge_kw=30.0,
            ),
        ],
        solar=SolarConfig(forecast_kw=np.zeros(1)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(1))],
        previous_plan=previous,
        proximal_weight=0.0,
        max_rate_kw=rate,
    )
    assert plan.status == "optimal"
    by = {b.name: float(b.discharge_kw[0]) for b in plan.batteries}
    assert abs(by["home"] - home_prev) <= rate + 1e-6
    assert abs(by["ev"] - ev_prev) <= rate + 1e-6


@pytest.mark.parametrize(
    ("names", "says"),
    [([], "at least one"), (["home", "home"], "unique names")],
    ids=["none", "duplicate"],
)
def test_a_plan_needs_named_batteries_each_once(names, says):
    """#467 stage 1: at least one battery, and each name once (the name is
    what the rate limit and every reader match on)."""
    with pytest.raises(ValueError, match=says):
        build_plan(
            periods=PeriodGrid(np.ones(2)),
            grid=GridConfig(
                import_price=np.full(2, 0.3),
                export_price=np.full(2, 0.05),
                import_limit_kw=10.0,
                export_limit_kw=10.0,
            ),
            batteries=[_pack(n, 0.95) for n in names],
            solar=SolarConfig(forecast_kw=np.zeros(2)),
        )


# --- #563 items 2-3, #779: availability, the away prefix, the shared charger ------


@SETTINGS
@given(st.booleans(), st.one_of(st.none(), st.integers(0, 20)))
def test_the_away_prefix_is_read_only_while_the_battery_is_away(available, until):
    """#563 item 2 and #779 (Mark Purcell): `available=False` gates the whole
    plan, or only the periods before `unavailable_until_period_index` when
    one is set (the car back within the hour), an index past the plan
    gating every period without failing. The index means nothing while the
    battery is available: it then trades as if no index were set."""
    n = _SWING_N
    plan = _swing_plan(_car(available=available, unavailable_until_period_index=until))
    assert plan.status == "optimal"
    (ev,) = plan.batteries
    if available:
        free = _swing_plan(_car())
        np.testing.assert_allclose(ev.charge_kw, free.batteries[0].charge_kw, atol=1e-6)
        np.testing.assert_allclose(
            ev.discharge_kw, free.batteries[0].discharge_kw, atol=1e-6
        )
        return
    gated = range(n) if until is None else range(min(until, n))
    for t in gated:
        assert ev.charge_kw[t] == 0.0 and ev.discharge_kw[t] == 0.0, t
    if until is not None and until < n // 2:
        assert _moves(ev, range(until, n)) > 1.0


def test_a_departure_past_the_plan_changes_nothing():
    """#563 item 3: a departure deadline beyond this solve's horizon is not
    this solve's to meet, so the plan is the plan without it."""
    base = _swing_plan(_car())
    later = _swing_plan(
        _car(must_have_soc_by_period_index=_SWING_N + 5, must_have_soc_kwh=39.0)
    )
    np.testing.assert_allclose(
        later.batteries[0].soc_kwh, base.batteries[0].soc_kwh, atol=1e-6
    )
    assert later.total_cost == pytest.approx(base.total_cost, abs=1e-6)


def _paid_to_import(*batteries, import_price=-1.0, export_price=-2.0):
    """#2031's reproduction: three hours, paid to import, nothing to serve."""
    return build_plan(
        periods=PeriodGrid(np.ones(3)),
        grid=GridConfig(
            import_price=np.full(3, import_price),
            export_price=np.full(3, export_price),
            import_limit_kw=28.0,
            export_limit_kw=10.0,
        ),
        batteries=list(batteries),
        solar=SolarConfig(forecast_kw=np.zeros(3)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(3))],
    )


def _small(name, **kw):
    return BatteryConfig(
        name=name,
        capacity_kwh=5.0,
        initial_soc_kwh=0.0,
        min_soc_kwh=0.0,
        max_soc_kwh=4.375,
        max_charge_kw=7.0,
        max_discharge_kw=7.0,
        charge_efficiency=0.875,
        discharge_efficiency=0.875,
        charge_cost=0.005,
        discharge_cost=0.005,
        salvage_value=0.0,
        **kw,
    )


@pytest.mark.parametrize(
    "groups",
    [
        ({}, {}),  # two independent batteries
        ({"shared_charger_group": "g", "shared_charger_max_kw": 7.0}, {}),  # one member
        ({"shared_charger_group": "g"}, {"shared_charger_group": "g"}),  # no ceiling
    ],
    ids=["no_group", "one_member", "no_ceiling"],
)
def test_only_a_shared_charger_with_a_ceiling_gets_the_binary(groups):
    """Independent batteries may still charge and discharge at once; that is
    real. A one-member group has its own battery binary, and a group with no
    declared ceiling has no cap row to share."""
    batteries = [_small("a", **groups[0]), _small("c", **groups[1])]
    assert network._shared_charger_groups(batteries) == {}
    plan = _paid_to_import(*batteries)
    assert not any(
        n.startswith("charger_dir_")
        for n in network._opposing_flow_binaries(plan, batteries)
    )


def test_a_plan_one_way_through_the_charger_needs_no_binary():
    """Charging pays and pays most now: both members fill, nothing flows back,
    and the plan is returned as first solved."""
    group = {"shared_charger_group": "g", "shared_charger_max_kw": 7.0}
    batteries = [_small("a", **group), _small("c", **group)]
    plan = build_plan(
        periods=PeriodGrid(np.ones(3)),
        grid=GridConfig(
            import_price=np.array([0.10, 0.11, 0.12]),
            export_price=np.zeros(3),
            import_limit_kw=28.0,
            export_limit_kw=10.0,
        ),
        batteries=batteries,
        solar=SolarConfig(forecast_kw=np.zeros(3)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(3))],
    )
    assert plan.status == "optimal"
    assert network._opposing_flow_binaries(plan, batteries) == frozenset()
    assert plan.direction_exclusivity == "not_needed"


@pytest.mark.parametrize("neighbour", [False, True])
@pytest.mark.parametrize("price", [(0.2, 0.2, 0.2), (0.5, 0.25, 0.4)])
def test_a_shut_period_is_degenerate_whatever_else_is_in_the_solve(price, neighbour):
    """#1981 (#492's flag): an adequacy period outside its window has a
    ceiling of 0, so its band is zero width and it is degenerate. The flag
    used to follow HiGHS's unclamped ranging instead, and a sheddable load
    in the same solve was enough to turn it off."""
    n = len(price)
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.asarray(price),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_idle_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        sheddable_loads=[
            SheddableLoadConfig(name="pool", forecast_kw=np.ones(n), shed_cost=0.3)
        ]
        if neighbour
        else None,
        adequacy_loads=[
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=3.0,
                target_kwh=1.0,
                deadline_period=0,
                earliest_period=0,
                shortfall_price=5.0,
            )
        ],
        adequacy_semi_continuous=False,
        compute_signals=True,
    )
    assert plan.status == "optimal"
    sig = plan.load_signals[-1]
    assert sig.name == "hws"
    for t in range(1, n):
        assert sig.degenerate[t], t
        assert sig.band_min_kw[t] == sig.band_max_kw[t] == pytest.approx(0.0)


@SETTINGS
@given(sites())
def test_a_battery_s_ranged_headroom_is_within_its_physical_headroom(site):
    """#1985 (#491's promise): what ranging says a battery could still move
    never exceeds what its limits and plan physically leave. HiGHS's bound
    ranging for a battery charging flat out read 0.32 kW of room it did not
    have."""
    plan = build_plan(
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
        compute_signals=True,
    )
    if plan.status != "optimal" or plan.grid_signals is None:
        return
    (bs,) = plan.battery_signals
    assert (bs.available_up_ranging_kw <= bs.available_up_kw + 1e-9).all()
    assert (bs.available_down_ranging_kw <= bs.available_down_kw + 1e-9).all()
    assert (bs.available_up_ranging_kw >= 0).all()
    assert (bs.available_down_ranging_kw >= 0).all()
