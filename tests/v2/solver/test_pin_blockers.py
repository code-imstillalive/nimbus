"""Why the previous plan's first period cannot be held (#1577) (#1837).

When last solve's period-0 setpoint is infeasible now, the explainer names
the period-0 constraint it breaks. Checked against the real LP: on every
site and pin below, the solver rejects the pin exactly when the explainer
names something, and each named constraint is the one built to be broken.
The explainer only reads; it never solves.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from solver import pin_blockers
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

N = 4


def _site(*, soc=20.0, export_limit=40.0, fixed_export=None, **battery):
    fields = {
        "name": "home",
        "capacity_kwh": 40.0,
        "initial_soc_kwh": soc,
        "min_soc_kwh": 4.0,
        "max_soc_kwh": 40.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 25.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.006,
        "discharge_cost": 0.006,
        "salvage_value": 0.2,
        **battery,
    }
    grid = GridConfig(
        import_price=np.full(N, 0.30),
        export_price=np.full(N, 0.05),
        import_limit_kw=40.0,
        export_limit_kw=export_limit,
    )
    if fixed_export is not None:
        fixed = np.full(N, np.nan)
        fixed[0] = fixed_export
        grid = dataclasses.replace(grid, fixed_export_kw=fixed)
    return {
        "periods": PeriodGrid(np.full(N, 0.5)),
        "grid": grid,
        "batteries": [BatteryConfig(**fields)],
        "solar": SolarConfig(forecast_kw=np.zeros(N)),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(N, 2.0))],
    }


def _status(site, pin, batteries=None):
    batteries = batteries or site["batteries"]
    pinned = [dataclasses.replace(batteries[0], period0_pin_net_kw=pin), *batteries[1:]]
    return build_plan(**{**site, "batteries": pinned}).status


def _blamed(site, pin, batteries=None):
    found = pin_blockers.period0_pin_blockers(
        pin,
        batteries=batteries or site["batteries"],
        grid=site["grid"],
        hours0=0.5,
        solar=site["solar"],
        loads=site["loads"],
    )
    return [b.split(":", 1)[0] for b in found]


def test_the_solver_rejects_a_pin_exactly_when_something_is_named():
    """Sound (a feasible pin is never blamed) and, on these sites, complete
    (an infeasible one is always explained)."""
    for site in [
        _site(),
        _site(soc=4.5),
        _site(soc=39.5),
        _site(export_limit=2.0),
        _site(fixed_export=5.0),
        _site(charge_power_curve=[(4.0, 10.0), (40.0, 1.0)]),
    ]:
        feasible = 0
        for pin in (-12.0, -9.0, -6.0, -4.0, -1.0, 0.0, 0.5, 2.0, 5.0, 15.0, 30.0):
            optimal = _status(site, pin) == "optimal"
            feasible += optimal
            assert (_blamed(site, pin) == []) is optimal, pin
        assert feasible >= 3


def _with_ev(site, *, group_kw=None, ev_kw=None):
    home = site["batteries"][0]
    ev = dataclasses.replace(home, name="ev")
    if ev_kw is not None:
        ev = dataclasses.replace(ev, max_charge_kw=ev_kw, max_discharge_kw=ev_kw)
    if group_kw is not None:
        home, ev = (
            dataclasses.replace(
                b, shared_charger_group="g", shared_charger_max_kw=group_kw
            )
            for b in (home, ev)
        )
    return [home, ev]


def test_a_shared_charger_is_checked_and_the_other_batterys_limits_never_blamed():
    site = _site()
    shared = _with_ev(site, group_kw=6.0)
    assert _status(site, -8.0, shared) == "infeasible"
    assert _blamed(site, -8.0, shared) == ["shared_charger_g"]
    small_ev = _with_ev(site, ev_kw=1.0)
    assert _status(site, 20.0, small_ev) == "optimal"
    assert _blamed(site, 20.0, small_ev) == []
    assert _blamed(site, 30.0, small_ev) == ["battery_max_discharge"]
    for batteries in (shared, small_ev):
        for pin in (-10.0, -6.0, -2.0, 0.0, 2.0, 8.0, 20.0, 25.0):
            optimal = _status(site, pin, batteries) == "optimal"
            assert (_blamed(site, pin, batteries) == []) is optimal, pin


def test_without_the_solves_risk_aversion_a_banded_load_is_read_loosely_and_still_sound():
    site = _site(soc=4.5)
    site["loads"] = [
        LoadConfig(
            name="house",
            # Period 0 differs from the rest, so only period 0 can be read.
            forecast_kw=np.array([2.0, 12.0, 12.0, 12.0]),
            lower_kw=np.array([1.0, 10.0, 10.0, 10.0]),
            upper_kw=np.array([4.0, 14.0, 14.0, 14.0]),
        )
    ]
    for pin in (-12.0, -4.0, 0.0, 0.5, 5.0, 30.0):
        if _status(site, pin) == "optimal":
            assert _blamed(site, pin) == [], pin
        else:
            assert _blamed(site, pin), pin
