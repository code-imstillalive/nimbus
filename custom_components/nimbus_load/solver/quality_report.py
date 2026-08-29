"""Ties tracking.py + regret.py + epr.py together into ONE real, live
"how good is our current dispatch, right now" report -- direct response
to a real, explicit ask (2026-08-17): "I think we should have a live
tracker of the regret value and EPR score on the screen as we keep
going through the solver so we know if it is doing better."

Each of the three underlying modules deliberately answers a DIFFERENT
question (see their own module docstrings for the full reasoning):
- regret.py / epr.py: was the ECONOMIC PLAN right (a committed dispatch
  trajectory vs. a perfect-foresight alternative)?
- tracking.py: did REALITY actually execute what was commanded (a
  commanded setpoint vs. the real measured output)?
Neither subsumes the other -- a genuinely good plan can still be poorly
executed (a real inverter handoff dropping delivery for 20-30s), and a
perfectly-tracked setpoint can still be economically wrong (a bad
forecast). This module reports both, plus the hourly regret breakdown,
as one coherent object -- not a new metric, just the assembly.

## The two-tier export bonus mechanic and what it means for scoring
(2026-08-17, real, found putting this module together)

`evaluate_realized_cost()` (regret.py) has no concept of
GridConfig.export_bonus_price/export_bonus_volume_kwh at all -- it
prices a trajectory's export at one flat `export_price_real` per
period, full stop. That's fine for J_ref (fully idle -- zero export can
never earn a bonus anyway, so scoring it against the base/spot rate
alone is already exactly correct, with NO special-casing needed: unlike
this project's earlier, pre-two-tier EPR analysis, which had to
manually exclude J_ref from a flat P2P credit by hand, the two-tier
mechanism's own `export_bonus[t] <= grid_export[t]` constraint makes
this correct automatically). It is NOT fine for J_ach or J_star, both
of which genuinely earn real P2P bonus revenue that a base-rate-only
evaluation would silently omit.

Two different, deliberately DIFFERENT fixes, chosen for what's actually
the MOST ACCURATE source available for each:
- J_ach (the real, ALREADY-REALIZED trajectory): the real settled P2P
  dollars for that exact day are DIRECTLY KNOWN (this project's own
  sensor.lv_v2_p2p_confirmed_history, sibling 116KAT-HA-AI repo) --
  using that REAL, ground-truth figure is strictly more accurate than
  re-deriving an estimate of it, so J_ach = (residual-only evaluation,
  base rate) MINUS (real known P2P dollars earned that day).
- J_star (the oracle, a HYPOTHETICAL perfect-foresight plan that never
  actually happened): no real settled figure exists for a trajectory
  that was never dispatched. build_plan()'s OWN internal LP objective
  (`plan.total_cost`) already correctly prices the two-tier bonus
  exactly as the solver itself understands it (that's what the bonus
  mechanism's own cost terms are FOR) -- so J_star is read directly
  from the oracle plan's own total_cost, not re-derived via
  evaluate_realized_cost() at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .epr import EPRResult, compute_epr
from .network import build_plan
from .regret import evaluate_realized_cost, hourly_regret_breakdown
from .tracking import TrackingResult, compute_tracking_fidelity, tracking_error_cost


@dataclass(frozen=True)
class QualityReport:
    """One real day's full "how good is our current dispatch" answer."""

    epr: EPRResult
    tracking: TrackingResult
    tracking_cost: float
    """$ cost of the tracking gap (commanded vs actual), priced at
    export_price_real -- see tracking.py's own tracking_error_cost()."""
    j_ref: float
    j_ach: float
    j_star: float
    hourly_regret: dict[int, float]
    """(actual - oracle) cost per hour, see hourly_regret_breakdown()'s
    own docstring for the rust/teal reading and the real, honest gap
    between this dict's own sum and (j_ach - j_star) whenever
    salvage_value is nonzero."""


def compute_quality_report(
    *,
    periods: PeriodGrid,
    # Base/spot price only -- NO export_bonus_price/volume_kwh set. Used
    # for J_ref and J_ach's own residual-only evaluation (see module
    # docstring for why each needs a different bonus treatment).
    grid_residual: GridConfig,
    # Same base price as grid_residual, PLUS export_bonus_price/
    # export_bonus_volume_kwh set -- used to compute the real oracle.
    grid_oracle: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
    timestamps: list,
    # The REAL, settled P2P revenue for this exact day (ground truth,
    # not modeled) -- see module docstring.
    real_p2p_dollars_earned: float,
    commanded_charge_kw: NDArray[np.float64],
    commanded_discharge_kw: NDArray[np.float64],
    actual_charge_kw: NDArray[np.float64],
    actual_discharge_kw: NDArray[np.float64],
    final_soc_kwh_actual: float,
) -> QualityReport:
    hours = periods.hours
    n = len(hours)
    zero = np.zeros(n)

    j_ref_result = evaluate_realized_cost(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        charge_committed_kw=zero,
        discharge_committed_kw=zero,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=battery.initial_soc_kwh,
        salvage_value=battery.salvage_value,
        grid_import_limit_kw=grid_residual.import_limit_kw,
        grid_export_limit_kw=grid_residual.export_limit_kw,
        terminal_value_breakpoints=battery.terminal_value_breakpoints,
        battery_min_soc_kwh=battery.min_soc_kwh,
    )
    j_ref = j_ref_result.total_cost

    j_ach_residual = evaluate_realized_cost(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        charge_committed_kw=actual_charge_kw,
        discharge_committed_kw=actual_discharge_kw,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=final_soc_kwh_actual,
        salvage_value=battery.salvage_value,
        grid_import_limit_kw=grid_residual.import_limit_kw,
        grid_export_limit_kw=grid_residual.export_limit_kw,
        terminal_value_breakpoints=battery.terminal_value_breakpoints,
        battery_min_soc_kwh=battery.min_soc_kwh,
    )
    j_ach = j_ach_residual.total_cost - real_p2p_dollars_earned

    oracle_plan = build_plan(
        periods=periods, grid=grid_oracle, battery=battery, solar=solar, loads=[load]
    )
    if not oracle_plan.is_optimal:
        msg = f"Oracle solve failed (status={oracle_plan.status}) -- should not happen with real, already-realized data unless genuinely infeasible"
        raise RuntimeError(msg)
    j_star = float(oracle_plan.total_cost)

    # Oracle's own per-period cost, for the hourly breakdown -- evaluated
    # the SAME residual-only way as j_ach_residual above (base rate,
    # zero bonus knowledge), for a fair, apples-to-apples HOURLY shape
    # comparison. This means the hourly dict's own sum will NOT equal
    # (j_ach - j_star) whenever real bonus revenue differs between the
    # two trajectories -- same honest, documented gap
    # hourly_regret_breakdown() itself already discloses for
    # salvage_value; stated here too rather than silently surprising a
    # caller who sums the dict and compares it to the headline EPR.
    oracle_residual = evaluate_realized_cost(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        charge_committed_kw=oracle_plan.battery_charge_kw,
        discharge_committed_kw=oracle_plan.battery_discharge_kw,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=float(oracle_plan.battery_soc_kwh[-1]),
        salvage_value=battery.salvage_value,
        grid_import_limit_kw=grid_residual.import_limit_kw,
        grid_export_limit_kw=grid_residual.export_limit_kw,
        terminal_value_breakpoints=battery.terminal_value_breakpoints,
        battery_min_soc_kwh=battery.min_soc_kwh,
    )
    hourly_regret = hourly_regret_breakdown(
        timestamps=timestamps,
        actual_cost_per_period=j_ach_residual.cost_per_period,
        oracle_cost_per_period=oracle_residual.cost_per_period,
    )

    epr_result = compute_epr(j_ref=j_ref, j_ach=j_ach, j_star=j_star)

    tracking_result = compute_tracking_fidelity(
        hours=hours,
        commanded_kw=commanded_discharge_kw - commanded_charge_kw,
        actual_kw=actual_discharge_kw - actual_charge_kw,
    )
    tracking_cost = tracking_error_cost(
        hours=hours,
        commanded_kw=commanded_discharge_kw - commanded_charge_kw,
        actual_kw=actual_discharge_kw - actual_charge_kw,
        export_price=grid_residual.export_price,
    )

    return QualityReport(
        epr=epr_result,
        tracking=tracking_result,
        tracking_cost=tracking_cost,
        j_ref=j_ref,
        j_ach=j_ach,
        j_star=j_star,
        hourly_regret=hourly_regret,
    )
