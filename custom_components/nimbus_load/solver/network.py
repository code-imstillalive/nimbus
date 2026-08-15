"""Assembles a Nimbus Solver LP from element configs, solves it, and
extracts a Plan.

Deliberately, this module NEVER writes anything anywhere -- no Modbus, no
HA entity, no automation trigger. build_plan() is a pure function: real
forecast/price inputs in, a Plan dataclass out. This is the "we will not
automate it to control anything... just to see how it behaves" boundary
the whole solver stage is currently operating inside -- enforced by this
module simply never importing anything HA-related, not by a runtime
check.

See the architecture sketch's own §2 for the three-layer design (Daily
Plan / Rolling Refinement / Safety Envelope) this module is the shared
core solve mechanism for -- this file implements ONE solve, callable at
whatever cadence/horizon a caller wants; layering is a caller-level
concern, not something build_plan() itself knows about.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SheddableLoadConfig, SolarConfig
from .lp import LPProblem, LPResult


@dataclass(frozen=True)
class SheddableLoadPlan:
    name: str
    served_kw: NDArray[np.float64]
    shed_kw: NDArray[np.float64]


@dataclass(frozen=True)
class Plan:
    """The solver's full output for one solve. Every array is indexed by
    period, same length as the PeriodGrid it was built from. `status` is
    always checked by the caller before trusting anything else here --
    see LPResult's own docstring for why infeasible/unbounded are real,
    expected outcomes this dataclass has to represent honestly, not paper
    over with zeros.
    """

    status: str
    periods: PeriodGrid
    battery_charge_kw: NDArray[np.float64]
    battery_discharge_kw: NDArray[np.float64]
    battery_soc_kwh: NDArray[np.float64]
    grid_import_kw: NDArray[np.float64]
    grid_export_kw: NDArray[np.float64]
    solar_used_kw: NDArray[np.float64]
    solar_curtailed_kw: NDArray[np.float64]
    sheddable_loads: list[SheddableLoadPlan]
    total_cost: float | None
    iterations: int

    @property
    def is_optimal(self) -> bool:
        return self.status == "optimal"


def _infeasible_plan(periods: PeriodGrid, status: str, iterations: int) -> Plan:
    """A well-formed but empty Plan for a non-optimal solve -- every array
    present (zero-filled), never omitted, so a caller can always safely
    index into a Plan's arrays without a separate None-check first; the
    REAL signal to check is `status`/`is_optimal`, not array presence.
    """
    n = periods.n_periods
    zeros = np.zeros(n)
    return Plan(
        status=status,
        periods=periods,
        battery_charge_kw=zeros,
        battery_discharge_kw=zeros,
        battery_soc_kwh=zeros,
        grid_import_kw=zeros,
        grid_export_kw=zeros,
        solar_used_kw=zeros,
        solar_curtailed_kw=zeros,
        sheddable_loads=[],
        total_cost=None,
        iterations=iterations,
    )


def build_plan(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    loads: list[LoadConfig] | None = None,
    sheddable_loads: list[SheddableLoadConfig] | None = None,
) -> Plan:
    """Build and solve one LP for the given horizon/inputs. Pure function --
    no I/O, no HA dependency, safe to call from anywhere including a plain
    local test script.
    """
    loads = loads or []
    sheddable_loads = sheddable_loads or []
    n = periods.n_periods
    hours = periods.hours

    for cfg in (solar, *loads, *sheddable_loads):
        arr_name = "forecast_kw"
        arr = getattr(cfg, arr_name)
        if len(arr) != n:
            label = getattr(cfg, "name", cfg.__class__.__name__)
            msg = f"{label}: forecast_kw has {len(arr)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)
    for arr, label in ((grid.import_price, "grid.import_price"), (grid.export_price, "grid.export_price")):
        if len(arr) != n:
            msg = f"{label} has {len(arr)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)

    p = LPProblem()

    charge = [p.add_variable(f"battery_charge_{t}", lb=0.0, ub=battery.max_charge_kw) for t in range(n)]
    discharge = [p.add_variable(f"battery_discharge_{t}", lb=0.0, ub=battery.max_discharge_kw) for t in range(n)]
    soc = [p.add_variable(f"battery_soc_{t}", lb=battery.min_soc_kwh, ub=battery.max_soc_kwh) for t in range(n)]
    grid_import = [p.add_variable(f"grid_import_{t}", lb=0.0, ub=grid.import_limit_kw) for t in range(n)]
    grid_export = [p.add_variable(f"grid_export_{t}", lb=0.0, ub=grid.export_limit_kw) for t in range(n)]
    solar_used = [p.add_variable(f"solar_used_{t}", lb=0.0, ub=float(solar.forecast_kw[t])) for t in range(n)]

    shed_vars: dict[str, list[str]] = {}
    for sl in sheddable_loads:
        max_shed = [(1.0 - sl.min_fraction) * float(sl.forecast_kw[t]) for t in range(n)]
        shed_vars[sl.name] = [p.add_variable(f"shed_{sl.name}_{t}", lb=0.0, ub=max_shed[t]) for t in range(n)]

    # ---- Cost terms ----
    for t in range(n):
        p.set_cost(grid_import[t], grid.import_price[t] * hours[t])
        p.set_cost(grid_export[t], -grid.export_price[t] * hours[t])
        p.set_cost(charge[t], battery.charge_cost * hours[t])
        p.set_cost(discharge[t], battery.discharge_cost * hours[t])
        for sl in sheddable_loads:
            p.set_cost(shed_vars[sl.name][t], sl.shed_cost * hours[t])
    # Salvage value: a one-time credit on the FINAL period's soc -- without
    # this, a finite-horizon LP has no reason to ever hold charge past the
    # last period it can see, and will always drain to its own min_soc on
    # the final tick (see the architecture sketch's own §6 "Salvage value,
    # in plain terms" explainer).
    p.set_cost(soc[n - 1], -battery.salvage_value)

    # ---- SoC dynamics ----
    for t in range(n):
        prev = battery.initial_soc_kwh if t == 0 else None
        terms = {
            soc[t]: 1.0,
            charge[t]: -battery.charge_efficiency * hours[t],
            discharge[t]: hours[t] / battery.discharge_efficiency,
        }
        if prev is None:
            terms[soc[t - 1]] = -1.0
            p.add_eq_constraint(terms, 0.0)
        else:
            p.add_eq_constraint(terms, prev)

    # ---- Power balance at the switchboard, every period ----
    plain_load_total = np.zeros(n)
    for load in loads:
        plain_load_total += load.forecast_kw

    for t in range(n):
        terms = {
            solar_used[t]: 1.0,
            discharge[t]: 1.0,
            grid_import[t]: 1.0,
            charge[t]: -1.0,
            grid_export[t]: -1.0,
        }
        rhs = plain_load_total[t]
        for sl in sheddable_loads:
            # served = forecast - shed, moved to the LHS as -shed (a
            # positive coefficient on the shed variable subtracts from
            # what the balance equation demands be supplied)
            terms[shed_vars[sl.name][t]] = 1.0
            rhs += float(sl.forecast_kw[t])
        p.add_eq_constraint(terms, rhs)

    result: LPResult = p.solve()
    if result.status != "optimal":
        return _infeasible_plan(periods, result.status, result.iterations)

    def _get(names: list[str]) -> NDArray[np.float64]:
        return p.values_of(result, names)

    plan_sheddable = [
        SheddableLoadPlan(
            name=sl.name,
            served_kw=sl.forecast_kw - _get(shed_vars[sl.name]),
            shed_kw=_get(shed_vars[sl.name]),
        )
        for sl in sheddable_loads
    ]

    solar_used_arr = _get(solar_used)
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=_get(charge),
        battery_discharge_kw=_get(discharge),
        battery_soc_kwh=_get(soc),
        grid_import_kw=_get(grid_import),
        grid_export_kw=_get(grid_export),
        solar_used_kw=solar_used_arr,
        solar_curtailed_kw=solar.forecast_kw - solar_used_arr,
        sheddable_loads=plan_sheddable,
        total_cost=result.objective,
        iterations=result.iterations,
    )
