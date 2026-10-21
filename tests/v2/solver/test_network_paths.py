"""Layer 1 of the rebuilt suite (#1837): `network.py` paths the 21 Oct fair
score found only the old tests checking (seed 1837, old 22/30, new 20/30).

Each states what the LP is for, with its own oracle: a plan imports up to
its own limit, a thermal load with no weather forecast is the flat model,
the spike override lifts a committed export only in period 0, and the
earliness tie-breaks (adequacy and thermal) cost nothing at the plan's
first instant.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan
from test_plan_invariants import SETTINGS, TOL, _num, sites


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
    st.integers(min_value=3, max_value=8),
    st.sampled_from([0.25, 0.5]),
    _num(0.5, 2.0),
    _num(3.0, 8.0),
)
def test_the_spike_override_lifts_a_committed_export_only_in_period_0(
    n, width, committed, spike_kw
):
    """#694: with the spike override armed, period 0's committed P2P export
    becomes a floor so the extra discharge can leave; every later committed
    period stays pinned at exactly its committed rate."""
    fixed = np.full(n, committed)
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=np.full(n, 0.3),
            export_price=np.full(n, 2.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            _idle_battery(
                capacity_kwh=40.0,
                initial_soc_kwh=30.0,
                max_soc_kwh=39.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                available=True,
                spike_override_discharge_kw=spike_kw,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 0.5))],
    )
    assert plan.status == "optimal"
    assert plan.grid_export_kw[0] >= committed - TOL
    for t in range(1, n):
        assert abs(plan.grid_export_kw[t] - committed) <= 1e-6, t


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
    st.integers(min_value=3, max_value=12),
    st.sampled_from([0.25, 0.5, 1.0]),
    _num(0.5, 3.0),
    _num(0.05, 0.5),
)
def test_an_adequacy_load_run_at_the_first_instant_costs_no_earliness(
    n, width, power, price
):
    """The earliness tie-break charges each kWh by how far into the plan it
    runs: at the plan's first instant that is nothing. A load that can run
    only in period 0 costs the same with the tie-break on or off."""

    def plan_with(budget):
        return build_plan(
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
            adequacy_loads=[
                AdequacyLoadConfig(
                    name="pool",
                    max_power_kw=power,
                    target_kwh=power * width,
                    earliest_period=0,
                    deadline_period=0,
                    shortfall_price=20.0,
                )
            ],
            adequacy_earliness_budget_kw=budget,
        )

    off, on = plan_with(0.0), plan_with(1.0)
    assert off.status == on.status == "optimal"
    assert abs(on.total_cost - off.total_cost) <= 1e-9 * max(1.0, abs(off.total_cost))


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
