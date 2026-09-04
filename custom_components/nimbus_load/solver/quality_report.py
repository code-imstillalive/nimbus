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
from datetime import UTC, datetime, timedelta

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
    j_ref_hourly: dict[str, dict[str, float]]
    j_ach_hourly: dict[str, dict[str, float]]
    j_star_hourly: dict[str, dict[str, float]]
    """Hourly reconstruction dicts, one per trajectory -- 24 rows for
    the normal, real-calendar-day case, more for a longer window (see
    `_hourly_means_by_key()`'s own docstring, nimbus issue #356 item 4:
    a window longer than 24h used to be silently folded onto the same
    24 hour-of-day buckets, blending distinct real days together).
    Reframed 2026-08-31 (direct ask): row-major, indexed by ISO local
    timestamp with the site tz offset (e.g. `'2026-08-30T00:00:00+10:00'`
    for Brisbane), each row a self-describing record with SEVEN entity
    fields (import_price_aud_per_kwh, export_price_aud_per_kwh, load_kw,
    solar_kw, battery_kw, grid_kw, soc_pct). Sign conventions:
    battery_kw + = charge / - = discharge, grid_kw + = import / - =
    export. Prices are identical across the three trajectories (same
    day's real settled prices) but included in every row so each row is
    self-describing. Empty periods (e.g. solar overnight) get 0.0, not
    None, so consumers can safely sum/mean without None-guards."""


def _hourly_means_by_key(
    *,
    hours: NDArray[np.float64],
    per_period: dict[str, NDArray[np.float64]],
    day_start: datetime,
) -> dict[str, dict[str, float]]:
    """Aggregate several n-period arrays to hourly rows, row-major,
    indexed by ISO local timestamp (`day_start` + h hours, tz-aware,
    formatted as e.g. `'2026-08-30T00:00:00+10:00'`). Each row is a
    self-describing record with one float per input key. Empty hours
    get 0.0, not None, so the resulting dict is safe to sum/mean
    without None guards -- the intended consumer is a Lovelace/
    apexcharts card, not a forensic per-period audit (the LP grid is
    still available on the parent Nimbus sensors for that use case).

    `hours` is periods.hours (per-period duration in hours, typically
    a np.full(n, 0.25) array). `day_start` is the tz-aware datetime
    the run is anchored on (period 0 == day_start, period n-1 ==
    day_start + cumulative-hours-so-far). The number of rows returned
    is however many real hours the window actually spans (`ceil(sum(
    hours))`) -- 24 for the normal, byte-identical-to-before daily
    case, more for a longer window.

    nimbus issue #356 (Mark Purcell), item 4: this used to hard-fold
    every period onto exactly 24 buckets via `% 24`, silently
    correct ONLY because `compute_daily_quality_report()` was, at the
    time, the only caller and always passed exactly one real calendar
    day. Issue #316's own `compute_quality_report` service (added in
    v0.94.42) lets a caller request an ARBITRARY window, including
    `allow_partial=True` windows longer than 24h (explicitly for
    diagnostics/backfill/A-B comparison, per `_compute_report_for_
    window()`'s own docstring) -- for any such window, the old `% 24`
    genuinely averaged DIFFERENT REAL CALENDAR DAYS' data into the same
    hour-of-day bucket (e.g. a 48h window's hour 24 and hour 0 landing
    in the same bucket), silently blending two distinct days' worth of
    prices/dispatch into one number with zero indication this happened.
    Now indexes by REAL ELAPSED HOUR from `day_start` (no modulo) --
    a <=24h window (the normal, and only previously-correct, case)
    produces byte-identical output to before this fix; a longer window
    now produces one honest row per real hour actually in it, instead
    of a silently-blended one.
    """
    # Cumulative hours from day-start, floored to an hour index. For a
    # uniform 15-min grid within one day this is
    # [0,0,0,0,1,1,1,1,...,23,23,23,23] -- identical to the pre-fix
    # values, since there's nothing to fold when the window IS <=24h.
    cum = np.cumsum(hours) - hours
    hour_index = np.floor(cum).astype(int)
    n_hours = int(hour_index.max()) + 1 if len(hour_index) else 0
    # Pre-build the ISO-format keys once. isoformat() on a tz-aware
    # datetime produces e.g. '2026-08-30T00:00:00+10:00' -- exactly the
    # shape a Lovelace/apexcharts card can parse straight back into a
    # Date via `new Date(key)`.
    #
    # nimbus issue #368: accumulated in UTC, not by naive wall-clock
    # `day_start + timedelta(hours=h)`. Adding a timedelta to a
    # ZoneInfo-aware datetime is pure wall-clock arithmetic -- across a
    # real DST transition day this either skips a real hour (spring-
    # forward, one key silently missing/wrong) or produces two IDENTICAL
    # keys for the repeated hour (fall-back, one real hour's data
    # silently overwrites the other's in the `means` dict below).
    # Converting day_start to UTC, stepping there, then converting each
    # instant back keeps the same local-ISO-string output shape while
    # making every key a genuinely distinct real hour, however many
    # hours the window spans. No-op for a UTC or DST-free zone
    # (Brisbane, this project's own reference household, never
    # observes DST).
    day_start_tzinfo = day_start.tzinfo
    day_start_utc = day_start.astimezone(UTC)
    hour_keys = [
        (day_start_utc + timedelta(hours=h)).astimezone(day_start_tzinfo).isoformat()
        for h in range(n_hours)
    ]
    # Pre-compute one hourly mean per (key, hour) so the row-major
    # assembly below is a plain lookup.
    means: dict[str, list[float]] = {}
    for key, arr in per_period.items():
        row: list[float] = []
        for h in range(n_hours):
            mask = hour_index == h
            if mask.any():
                row.append(round(float(arr[mask].mean()), 4))
            else:
                row.append(0.0)
        means[key] = row
    # Assemble row-major: one dict entry per hour, containing one
    # float per input key. Iteration order of `per_period` (Python 3.7+
    # ordered) is preserved inside each row, so callers can rely on
    # import_price / export_price / load_kw / solar_kw / battery_kw /
    # grid_kw / soc_pct staying in that order when the caller passes
    # them in that order (see compute_quality_report below).
    out: dict[str, dict[str, float]] = {}
    for h in range(n_hours):
        out[hour_keys[h]] = {key: means[key][h] for key in per_period}
    return out


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
        degradation_cost_per_kwh=battery.degradation_cost_per_kwh,
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
        degradation_cost_per_kwh=battery.degradation_cost_per_kwh,
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

    # 24-hour reconstruction dicts, one per trajectory (2026-08-31, direct
    # ask, full state reconstruction on the flattened J_ref/J_ach/J_star
    # child sensors). Prices are the same across the three trajectories
    # -- same day's real settled prices -- but included in every
    # trajectory dict so each dict is self-describing when consumed as a
    # sensor attribute. Sign conventions: battery_kw + = charge / - =
    # discharge, grid_kw + = import / - = export. Grid_kw is derived,
    # not measured, to keep the reconstruction identity (load - solar +
    # battery_charge - battery_discharge = grid) exact by construction.
    # SoC is only meaningfully defined for j_ach (measured) and j_star
    # (oracle plan). For j_ref (idle) it stays flat at the initial value.
    j_ref_battery_net_kw = zero  # idle trajectory: battery does nothing
    j_ach_battery_net_kw = actual_charge_kw - actual_discharge_kw
    j_star_battery_net_kw = np.asarray(
        oracle_plan.battery_charge_kw - oracle_plan.battery_discharge_kw,
        dtype=np.float64,
    )
    # SoC per trajectory: j_ref flat; j_ach as measured (approximated by
    # integrating the actual battery net kW from the initial SoC using
    # the sqrt-split efficiencies); j_star from the oracle plan directly.
    initial_soc_kwh = battery.initial_soc_kwh
    capacity_kwh = battery.capacity_kwh
    # Actual per-period delta_kwh = charge * eta_c * dt - discharge * dt / eta_d.
    dt = hours
    ach_delta = (
        actual_charge_kw * battery.charge_efficiency * dt
        - actual_discharge_kw * dt / battery.discharge_efficiency
    )
    j_ach_soc_kwh = initial_soc_kwh + np.cumsum(ach_delta)
    j_star_soc_kwh = np.asarray(oracle_plan.battery_soc_kwh, dtype=np.float64)

    # SoC arrays as % (0..100) for consumer readability. Capacity 0 =>
    # no battery configured, keep the array at 0.0 rather than dividing.
    def _soc_pct(soc_kwh: NDArray[np.float64]) -> NDArray[np.float64]:
        if capacity_kwh <= 0.0:
            return np.zeros(n)
        return soc_kwh / capacity_kwh * 100.0

    j_ref_soc_pct = np.full(n, _soc_pct(np.array([initial_soc_kwh]))[0])
    j_ach_soc_pct = _soc_pct(j_ach_soc_kwh)
    j_star_soc_pct = _soc_pct(j_star_soc_kwh)
    # Grid_kw derived from the reconstruction identity, per trajectory.
    # For j_star the LP uses MODEL solar/load (solar.forecast_kw /
    # load.forecast_kw) -- same inputs as j_ref/j_ach here because in
    # compute_daily_quality_report()'s calling site both are set from
    # yesterday's REAL measured history, but callers who pass a genuine
    # forecast for j_star will get the LP's own view of grid_kw.
    j_ref_grid_kw = load.forecast_kw - solar.forecast_kw + zero  # idle battery
    j_ach_grid_kw = load.forecast_kw - solar.forecast_kw + j_ach_battery_net_kw
    j_star_grid_kw = load.forecast_kw - solar.forecast_kw + j_star_battery_net_kw
    # `timestamps[0]` is period 0's tz-aware datetime, always the
    # day-start anchor for the daily-quality run (see
    # compute_daily_quality_report()). Passed through to
    # _hourly_means_by_key so each hourly row is keyed by the real
    # local ISO timestamp, not a bare '0'..'23' hour index.
    day_start = timestamps[0]
    j_ref_hourly = _hourly_means_by_key(
        hours=hours,
        per_period={
            "import_price_aud_per_kwh": grid_residual.import_price,
            "export_price_aud_per_kwh": grid_residual.export_price,
            "load_kw": load.forecast_kw,
            "solar_kw": solar.forecast_kw,
            "battery_kw": j_ref_battery_net_kw,
            "grid_kw": j_ref_grid_kw,
            "soc_pct": j_ref_soc_pct,
        },
        day_start=day_start,
    )
    j_ach_hourly = _hourly_means_by_key(
        hours=hours,
        per_period={
            "import_price_aud_per_kwh": grid_residual.import_price,
            "export_price_aud_per_kwh": grid_residual.export_price,
            "load_kw": load.forecast_kw,
            "solar_kw": solar.forecast_kw,
            "battery_kw": j_ach_battery_net_kw,
            "grid_kw": j_ach_grid_kw,
            "soc_pct": j_ach_soc_pct,
        },
        day_start=day_start,
    )
    j_star_hourly = _hourly_means_by_key(
        hours=hours,
        per_period={
            "import_price_aud_per_kwh": grid_residual.import_price,
            "export_price_aud_per_kwh": grid_residual.export_price,
            "load_kw": load.forecast_kw,
            "solar_kw": solar.forecast_kw,
            "battery_kw": j_star_battery_net_kw,
            "grid_kw": j_star_grid_kw,
            "soc_pct": j_star_soc_pct,
        },
        day_start=day_start,
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
        j_ref_hourly=j_ref_hourly,
        j_ach_hourly=j_ach_hourly,
        j_star_hourly=j_star_hourly,
    )
