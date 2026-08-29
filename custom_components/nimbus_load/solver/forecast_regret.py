"""Forecast-quality regret decomposition -- isolates Nimbus's own forecast
contribution to household EPR from the LP/execution layers, per Mark
Purcell's own four-way decomposition (nimbus issue #273, 2026-08-29):
topology error, FORECAST error (this module), optimisation error,
execution error.

## Why this is a separate module from regret.py

regret.py already answers "how did the REAL, committed dispatch compare
to a perfect-foresight oracle" (J_ach vs J*) -- but that comparison can
never separate "the forecast was wrong" from "the optimizer/control loop
did something suboptimal even with the forecast it had." This module
answers a narrower, different question: given the SAME real day, the
SAME LP, the SAME battery/grid config, does using Nimbus's own forecast
produce a genuinely better plan than a naive persistence forecast would
have -- holding everything else fixed?

## The three-scenario method

Every scenario re-solves the IDENTICAL LP (same periods, grid, battery)
varying ONLY which solar/load array it's built from, then evaluates the
resulting COMMITTED battery trajectory against the SAME real ground
truth via evaluate_realized_cost() -- never a plan's own internally-
reported total_cost, which prices its own (possibly wrong) forecast, not
reality. This is the same "recompute grid_import/export from the REAL
balance equation" discipline regret.py's own module docstring already
established -- a plan built on a wrong forecast still commits to a real
battery_charge_kw/discharge_kw trajectory, and reality doesn't care what
the plan assumed load/solar would be.

- **J_star (oracle)**: perfect foresight -- the plan is built from the
  SAME real solar/load it's then evaluated against. No other trajectory
  can beat this; it's the same J* every other module in this package
  already uses as the universal upper bound.
- **J_forecast**: the plan is built from Nimbus's own forecast, then
  evaluated against real conditions. (J_forecast - J_star) is the
  forecast-attributable regret -- if the LP/execution logic itself were
  flawed, that flaw is present in BOTH this scenario and the oracle
  scenario (both use build_plan()/evaluate_realized_cost() identically),
  so it cancels out of the difference. What's left is attributable to
  the forecast alone being imperfect.
- **J_persistence**: the same plan-then-evaluate process, but built from
  a naive persistence forecast (e.g. "same time yesterday") instead of
  Nimbus's own. (J_persistence - J_star) is the regret a household would
  see doing nothing smarter than persistence.

## Nimbus's own real, measurable value-add

nimbus_value_add_dollars = J_persistence - J_forecast (more negative
J_forecast, i.e. lower cost, is a real improvement -- so a POSITIVE
value here means Nimbus's forecast genuinely beat naive persistence for
this real day; the "Nimbus contribution to household EPR" nimbus
issue #273 asks about directly). This is deliberately NOT
forecast_regret alone (J_forecast - J_star) -- an absolute regret number
has no comparison point a household can act on ("regret is $4.20 -- is
that good?"); the persistence delta gives one ("Nimbus saved you $2.10
today compared to a naive forecast").

## What this module deliberately does NOT do

- Does not capture or store Nimbus's own historical forecast snapshots
  -- the caller (a real writer/coordinator) is responsible for supplying
  solar_forecast_kw/load_forecast_kw for the day being scored, however
  it sources them (a captured live snapshot, or a hypothetical re-
  prediction). This module is a pure, portable computation -- no HA
  dependency, no I/O, safe to call from a plain local test script,
  matching every other module in this package.
- Does not define what "persistence" means -- solar_persistence_kw/
  load_persistence_kw are caller-supplied too. The obvious, zero-
  dependency choice (same-hour-yesterday, reconstructed from real
  recorder history alone) is documented here as the RECOMMENDED default
  for a real writer, not hardcoded into this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .network import build_plan
from .regret import evaluate_realized_cost


@dataclass(frozen=True)
class ForecastRegretResult:
    """One real day's forecast-quality decomposition. All three J values
    are RealizedCost.total_cost -- real dollars, evaluated against the
    SAME real ground truth (see this module's own docstring for why).
    """

    j_star: float
    """Perfect-foresight oracle -- the universal lower bound (best possible)."""
    j_forecast: float
    """Nimbus's own forecast, plan evaluated against real conditions."""
    j_persistence: float
    """Naive persistence forecast, plan evaluated the same way."""

    @property
    def forecast_regret_dollars(self) -> float:
        """J_forecast - J_star. The loss attributable to Nimbus's own
        forecast being imperfect, with LP/execution quality held equal
        across both scenarios (see module docstring). >= 0 for any
        correctly-modeled LP -- the oracle can never be beaten.
        """
        return self.j_forecast - self.j_star

    @property
    def persistence_regret_dollars(self) -> float:
        """J_persistence - J_star. What a household relying on naive
        persistence alone would have lost that day, for the same
        real-world reason forecast_regret_dollars is >= 0.
        """
        return self.j_persistence - self.j_star

    @property
    def nimbus_value_add_dollars(self) -> float:
        """J_persistence - J_forecast. Positive means Nimbus's real
        forecast beat naive persistence for this real day -- the direct,
        actionable "what did Nimbus's forecast quality actually save you
        today" figure (see module docstring for why this, not the bare
        forecast_regret_dollars, is the number worth publishing).
        """
        return self.j_persistence - self.j_forecast


def _evaluate_scenario(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar_plan_kw: NDArray[np.float64],
    load_plan_kw: NDArray[np.float64],
    solar_real_kw: NDArray[np.float64],
    load_real_kw: NDArray[np.float64],
) -> float:
    """Build a plan from (solar_plan_kw, load_plan_kw), then price its
    OWN committed battery trajectory against REAL conditions -- never
    the plan's own total_cost, which prices whatever it was built
    against, not reality (see module docstring).
    """
    plan = build_plan(
        periods=periods,
        grid=grid,
        battery=battery,
        solar=SolarConfig(forecast_kw=solar_plan_kw),
        loads=[LoadConfig(name="whole_house", forecast_kw=load_plan_kw)],
    )
    if not plan.is_optimal:
        msg = (
            f"Forecast-regret scenario solve failed (status={plan.status}) -- "
            "this should not happen with real battery/grid config unless the "
            "scenario is genuinely infeasible"
        )
        raise RuntimeError(msg)
    result = evaluate_realized_cost(
        hours=periods.hours,
        load_real_kw=load_real_kw,
        solar_real_kw=solar_real_kw,
        import_price_real=grid.import_price,
        export_price_real=grid.export_price,
        charge_committed_kw=plan.battery_charge_kw,
        discharge_committed_kw=plan.battery_discharge_kw,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=float(plan.battery_soc_kwh[-1]),
        salvage_value=0.0,
        grid_import_limit_kw=grid.import_limit_kw,
        grid_export_limit_kw=grid.export_limit_kw,
    )
    return result.total_cost


def compute_forecast_regret(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar_real_kw: NDArray[np.float64],
    load_real_kw: NDArray[np.float64],
    solar_forecast_kw: NDArray[np.float64],
    load_forecast_kw: NDArray[np.float64],
    solar_persistence_kw: NDArray[np.float64],
    load_persistence_kw: NDArray[np.float64],
) -> ForecastRegretResult:
    """Compute the full three-scenario forecast-quality decomposition for
    one real, already-elapsed day. See module docstring for the full
    method and what each result field means.

    Deliberately uses salvage_value=0.0 for every scenario -- same
    reasoning as the retrospective quality writer's own 2026-08-29 fix
    (see regret.py's evaluate_realized_cost() docstring): crediting
    energy still in the battery at day-close is a guess about tomorrow's
    value this single-day comparison has no honest basis for making, and
    would distort the comparison identically to how it broke EPR there.
    """
    oracle_charge_kw, oracle_discharge_kw, oracle_final_soc_kwh = (
        _oracle_dispatch_via_real(
            periods=periods, grid=grid, battery=battery,
            solar_real_kw=solar_real_kw, load_real_kw=load_real_kw,
        )
    )
    j_star = evaluate_realized_cost(
        hours=periods.hours,
        load_real_kw=load_real_kw,
        solar_real_kw=solar_real_kw,
        import_price_real=grid.import_price,
        export_price_real=grid.export_price,
        charge_committed_kw=oracle_charge_kw,
        discharge_committed_kw=oracle_discharge_kw,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=oracle_final_soc_kwh,
        salvage_value=0.0,
        grid_import_limit_kw=grid.import_limit_kw,
        grid_export_limit_kw=grid.export_limit_kw,
    ).total_cost

    j_forecast = _evaluate_scenario(
        periods=periods, grid=grid, battery=battery,
        solar_plan_kw=solar_forecast_kw, load_plan_kw=load_forecast_kw,
        solar_real_kw=solar_real_kw, load_real_kw=load_real_kw,
    )
    j_persistence = _evaluate_scenario(
        periods=periods, grid=grid, battery=battery,
        solar_plan_kw=solar_persistence_kw, load_plan_kw=load_persistence_kw,
        solar_real_kw=solar_real_kw, load_real_kw=load_real_kw,
    )

    return ForecastRegretResult(
        j_star=j_star, j_forecast=j_forecast, j_persistence=j_persistence
    )


def _oracle_dispatch_via_real(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar_real_kw: NDArray[np.float64],
    load_real_kw: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64], float]:
    """Thin wrapper matching regret.py's own oracle_dispatch() shape --
    kept local rather than importing oracle_dispatch() directly so this
    module's own real solar/load naming stays explicit at the call site.
    """
    plan = build_plan(
        periods=periods,
        grid=grid,
        battery=battery,
        solar=SolarConfig(forecast_kw=solar_real_kw),
        loads=[LoadConfig(name="whole_house", forecast_kw=load_real_kw)],
    )
    if not plan.is_optimal:
        msg = (
            f"Oracle solve failed (status={plan.status}) -- should not happen "
            "with real, already-realized data unless genuinely infeasible"
        )
        raise RuntimeError(msg)
    return (
        plan.battery_charge_kw,
        plan.battery_discharge_kw,
        float(plan.battery_soc_kwh[-1]),
    )
