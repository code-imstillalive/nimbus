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
from solver import lp as _solver_lp
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
from test_plan_invariants import SETTINGS, _num


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


def test_a_circuit_naming_an_unknown_load_is_refused_by_name():
    # test_a_circuit_naming_an_unknown_load_is_refused_by_name: A circuit member that is not a configured adequacy load is a caller
    _solver_lp._lex_calibration_failed_until = 0.0
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

    # test_a_spike_override_keeps_the_floor_on_the_free_part: Mark Purcell, #1924: at period 0 under a spike override the P2P
    _solver_lp._lex_calibration_failed_until = 0.0
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


@SETTINGS
@given(
    st.integers(1, 6),
    st.sampled_from([0.5, 1.0]),
    _num(0.5, 4.0),
    _num(0.0, 1.0),
    _num(-5.0, 30.0),
)
def test_a_thermal_deadline_is_hard_and_relaxed_only_when_out_of_reach(
    deadline, width, power, decay, margin
):
    """#774: the hot-water guarantee broke five times as a bundle of soft
    costs (#726, #741, #733, #769, #770/#782), so it is a hard row now. At
    full power the #897 idle scaling removes decay, so the most the tank can
    reach by its deadline is exactly initial + rate x power x hours. A
    target within that is met (the single solve is feasible and the plan
    never relaxes); one beyond it makes the single solve infeasible, and
    `build_plan` relaxes that load by name and still plans."""
    n = deadline + 2
    reach = 20.0 + 2.0 * power * width * (deadline + 1)
    target = reach - margin
    if abs(margin) < 1e-3:
        return
    kw = {
        "periods": PeriodGrid(np.full(n, width)),
        "grid": GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        "batteries": [_idle_battery()],
        "solar": SolarConfig(forecast_kw=np.zeros(n)),
        "loads": [LoadConfig(name="load", forecast_kw=np.ones(n))],
        "thermal_loads": [
            ThermalLoadConfig(
                name="hws",
                max_power_kw=power,
                initial_temperature_c=20.0,
                target_temperature_c=target,
                earliest_period=0,
                deadline_period=deadline,
                heating_rate_c_per_kwh=2.0,
                idle_decay_c_per_hour=decay,
            )
        ],
    }
    once = network._build_plan_once(**kw)
    plan = build_plan(**kw)
    assert plan.status == "optimal"
    if margin > 0:
        assert once.status == "optimal"
        assert plan.thermal_guarantee_relaxed == []
        assert plan.thermal_loads[0].temperature_c[deadline] >= target - 1e-5
    else:
        assert once.status == "infeasible"
        assert plan.thermal_guarantee_relaxed == ["hws"]


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


def test_a_plan_needs_named_batteries_each_once():
    # test_a_plan_needs_named_batteries_each_once: #467 stage 1: at least one battery, and each name once (the name is
    _solver_lp._lex_calibration_failed_until = 0.0
    for names, says in [([], "at least one"), (["home", "home"], "unique names")]:
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

    # test_a_departure_past_the_plan_changes_nothing: #563 item 3: a departure deadline beyond this solve's horizon is not
    _solver_lp._lex_calibration_failed_until = 0.0
    base = _swing_plan(_car())
    later = _swing_plan(
        _car(must_have_soc_by_period_index=_SWING_N + 5, must_have_soc_kwh=39.0)
    )
    np.testing.assert_allclose(
        later.batteries[0].soc_kwh, base.batteries[0].soc_kwh, atol=1e-6
    )
    assert later.total_cost == pytest.approx(base.total_cost, abs=1e-6)

    # test_only_a_shared_charger_with_a_ceiling_gets_the_binary: Independent batteries may still charge and discharge at once; that is
    _solver_lp._lex_calibration_failed_until = 0.0
    for groups in [
        ({}, {}),  # two independent batteries
        ({"shared_charger_group": "g", "shared_charger_max_kw": 7.0}, {}),  # one member
        ({"shared_charger_group": "g"}, {"shared_charger_group": "g"}),  # no ceiling
    ]:
        batteries = [_small("a", **groups[0]), _small("c", **groups[1])]
        assert network._shared_charger_groups(batteries) == {}
        plan = _paid_to_import(*batteries)
        assert not any(
            n.startswith("charger_dir_")
            for n in network._opposing_flow_binaries(plan, batteries)
        )

    # test_a_shut_period_is_degenerate_whatever_else_is_in_the_solve: #1981 (#492's flag): an adequacy period outside its window has a
    _solver_lp._lex_calibration_failed_until = 0.0
    for neighbour in [False, True]:
        for price in [(0.2, 0.2, 0.2), (0.5, 0.25, 0.4)]:
            _solver_lp._lex_calibration_failed_until = 0.0
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
                    SheddableLoadConfig(
                        name="pool", forecast_kw=np.ones(n), shed_cost=0.3
                    )
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


# --- #563 items 2-3, #779: availability, the away prefix, the shared charger ------


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
