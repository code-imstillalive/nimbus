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

`evaluate_realized_cost_multi()` (regret.py) prices a trajectory's
export at one flat `export_price_real` per period unless it is handed
the bonus terms explicitly.

**This block used to claim that was "already exactly correct" for J_ref,
"fully idle -- zero export can never earn a bonus anyway". That premise
was false, and it is what kept nimbus issue #1015 invisible.** J_ref
holds the BATTERY idle; it does not hold the HOUSE idle. Its grid
balance is still `load - solar`, so whenever solar surplus exceeds load
J_ref exports exactly like any other trajectory -- and if that surplus
lands inside a committed P2P window, the household would genuinely have
earned the premium on it.

The reference household hid this for a season: in September, solar has
finished by ~18:00 and the committed window is 17:00-24:00, so J_ref
imports through the whole window and there is no premium to credit.
Longer days, a daytime P2P block, a larger array, or a low evening load
all break that coincidence. Since J_ref sits in BOTH the numerator and
the denominator of `EPR = (J_ref - J_ach) / (J_ref - J_star)`,
understating it flatters the Solver -- the direction that costs trust
rather than money.

As of #1015, J_ref is given the same bonus terms J_star's own
`build_plan()` reads, via `evaluate_realized_cost_multi(
export_bonus_grid=...)`. The governing principle is **the two
COUNTERFACTUALS share one model; the ACTUAL keeps its real money** --
deliberately narrower than #1015's own first suggestion of pricing all
three identically, which would have replaced J_ach's real settled
dollars with a model of them.

So the three trajectories, chosen for the MOST ACCURATE source
available to each:
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
- J_ref (battery idle, but the HOUSE still running): also hypothetical,
  so it gets the same modelled treatment J_star does, applied to
  whatever it actually exports --
  `p2p_export.realized_export_bonus_credit()` allocates each real
  calendar day's capped volume to that day's highest-premium periods,
  which is what the revenue-maximising LP does with the same inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .epr import EPRResult, compute_epr
from .network import build_plan
from .p2p_export import realized_export_bonus_credit
from .regret import evaluate_realized_cost_multi, hourly_regret_breakdown
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
    None, so consumers can safely sum/mean without None-guards.

    nimbus #768/#585 (Mark Purcell): `battery_kw`/`soc_pct` are FLEET
    AGGREGATES whenever `compute_quality_report()` is given more than
    one battery (home + EV `battery_participant`s) -- `battery_kw` is
    the SUM of every participant's own net charge-minus-discharge kW,
    `soc_pct` is `sum(soc_kwh across every participant) /
    sum(capacity_kwh across every participant) * 100`. This is the same
    aggregation `Plan.battery_charge_kw`/`battery_soc_kwh` already use
    for the LIVE forward solve (nimbus #467) -- consistent with the
    scorer now answering "how did the whole storage fleet do", not just
    the home pack. A genuinely PER-PARTICIPANT breakdown of these
    hourly rows is deliberately not built in this pass (see
    `compute_quality_report()`'s own docstring for the full scope
    note) -- single-battery installs (still the common case) see a
    byte-identical result to before this change."""

    p2p_commitment_shortfall_kwh: float = 0.0
    """Energy a committed P2P block asked for and the day did not
    deliver (nimbus issue #1001), summed over every committed period.

    Exists because the oracle stopped refusing to consider an
    under-delivered hour. It had to: holding the oracle to a commitment
    the day did not meet makes the comparison impossible, and on a real
    install produced regret -$4.95 with `j_star` worse than doing
    nothing. The missed commitment is a genuine, separate signal, so it
    is reported directly rather than smuggled into regret as an
    infeasible oracle -- which made it visible only by noticing regret
    had gone impossible.

    0.0 means either no P2P commitment is configured or every committed
    hour was met. Defaulted so every existing construction site and test
    predating this field is unaffected."""

    j_star_evaluator: float = 0.0
    """The ORACLE's own plan, repriced through the same path `j_ach`
    takes (nimbus issue #1081).

    `j_star` is `oracle_plan.total_cost` -- the LP's own objective.
    `j_ach` is `evaluate_realized_cost_multi()` minus the real settled
    P2P dollars. Two different computations, and nothing has ever
    checked they agree.

    `j_star <= j_ach` is supposed to hold by construction, because the
    oracle's feasible set contains the achieved trajectory. But that is
    an argument about TRAJECTORIES. It only carries over to the NUMBERS
    if both are priced by the same arithmetic, and an LP objective
    carries terms an evaluator does not -- the soft-SoC penalty, slack
    penalties, and the export bonus as a variable the LP CHOOSES rather
    than a credit allocated after the fact.

    So a disagreement between the two paths can manufacture an
    impossible result out of two individually correct answers. This
    field is what makes that visible instead of inferred."""

    j_star_path_delta: float = 0.0
    """`j_star - j_star_evaluator` -- how far the LP objective and the
    evaluator disagree about the identical trajectory (nimbus #1081).

    Near zero means the two paths agree, so a negative regret is a real
    modelling problem and should be chased as one. Materially nonzero
    means the COMPARISON is the problem, and says by how much.

    Built to settle a specific open question rather than as a general
    metric: 15 Sep 2026 scored `epr_pct` 100.11 with `regret_dollars`
    -0.0214 on a ~$15 objective, and the leading explanation was
    "probably LP tolerance". That is exactly the kind of tidy story this
    project has had refuted by measurement several times, so it gets
    measured. `p2p_export.realized_export_bonus_credit()`'s own
    docstring already names the risk: *"Both must answer the same
    question the same way, or a counterfactual scored here is not
    comparable to one the LP produced."*"""


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


# nimbus issue #956: BatteryConfig rejects min_soc_kwh <= 0, so a
# reconstruction that genuinely reaches 0.0 kWh still needs a
# representable floor. One watt-hour is far below any real dispatch
# decision and keeps the LP well-posed.
_MIN_POSITIVE_SOC_KWH = 1e-3


def _soc_envelope_containing_achieved(
    battery: BatteryConfig,
    actual_charge_kw: NDArray[np.float64],
    actual_discharge_kw: NDArray[np.float64],
    hours: NDArray[np.float64],
) -> tuple[float, float]:
    """SoC bounds for the ORACLE that contain the achieved trajectory
    (nimbus issue #956, household decision 2026-09-16: "go with B").

    The defect: a real install published EPR 103.66% with regret
    -$0.7307 -- the achieved dispatch pricing out CHEAPER than perfect
    foresight, which the oracle's own construction is supposed to make
    impossible.

    It is possible because the two sides were not playing the same game.
    The oracle is an LP bound by this battery's configured envelope; the
    achieved side is a reconstruction from measured power, bound by
    nothing. On the day in question the achieved trajectory finished at
    **0.443%** against a configured 2.0% floor -- 1.90 kWh the oracle was
    forbidden to sell and the reconstruction sold anyway. Beat an
    opponent who is not allowed to touch what you just spent, and of
    course you win.

    **Two ways to fix that, and this is deliberately the second.**
    Clamping the achieved integration (option A) forces regret >= 0 by
    editing what the battery actually did, changes `j_ach` -- the
    headline achieved-cost figure -- on every install and every rescored
    day, and hides the very signal the anomaly carries. Widening the
    oracle instead leaves `j_ach` untouched and fixes the COMPARISON,
    which is what was actually broken.

    **Why this restores the invariant rather than merely improving it.**
    Once the achieved trajectory lies inside the oracle's feasible set,
    a cost-minimising oracle can always do at least as well as it, so
    `j_star <= j_ach` and `regret >= 0` follows by construction rather
    than by hoping the numbers behave.

    **What it means, stated plainly**: the oracle becomes "the best
    achievable given what this system demonstrably can do", instead of
    "the best achievable under limits the system does not actually
    honour". That is the more useful question of the two, and it is the
    one a household is really asking.

    Physical bounds still win. `BatteryConfig.__post_init__` requires
    `0 < min_soc <= max_soc <= capacity`, and a trajectory reconstructed
    as leaving `[0, capacity]` is sensor nonsense rather than a real
    state the oracle should be asked to match -- so the widening is
    clamped there. A trajectory that escapes even that is left to the
    #956 reliability flag, which is the honest outcome: the comparison
    genuinely cannot be trusted and now says so.
    """
    delta = (
        actual_charge_kw * battery.charge_efficiency * hours
        - actual_discharge_kw * hours / battery.discharge_efficiency
    )
    achieved = battery.initial_soc_kwh + np.cumsum(delta)
    # The initial SoC is part of the trajectory the oracle has to be able
    # to start from, so it is included alongside the solved points.
    lowest = float(min(achieved.min(), battery.initial_soc_kwh))
    highest = float(max(achieved.max(), battery.initial_soc_kwh))

    # Strictly positive: __post_init__ rejects min_soc_kwh <= 0, and a
    # reconstruction reaching exactly 0.0 is real enough to want to keep
    # representable rather than rejected.
    floor = max(_MIN_POSITIVE_SOC_KWH, min(battery.min_soc_kwh, lowest))
    ceiling = min(battery.capacity_kwh, max(battery.max_soc_kwh, highest))
    # Degenerate only if capacity itself is below the configured floor,
    # which is a misconfiguration this function must not turn into a
    # crash.
    floor = min(floor, ceiling)
    return floor, ceiling


def _achieved_grid_export_kw(
    *,
    hours: NDArray[np.float64],
    load_kw: NDArray[np.float64],
    solar_kw: NDArray[np.float64],
    actual_charge_kw: list[NDArray[np.float64]],
    actual_discharge_kw: list[NDArray[np.float64]],
) -> NDArray[np.floating[Any]]:
    """What the day actually exported, per period.

    The same reconstruction regret.py's own evaluator uses for J_ach, so
    "what the day exported" means one thing across the comparison rather
    than two nearly-identical definitions that can drift. Both the
    oracle's export band (nimbus issue #956) and the commitment
    shortfall (#1001) read it, which is exactly why it is a function
    rather than two inline copies.
    """
    # Annotated loosely on purpose: numpy widens float64 to floating[Any]
    # across an accumulating `+`, the same long-standing friction this
    # module already documents for `zero` elsewhere.
    total_charge_kw: NDArray[np.floating[Any]] = np.zeros(len(hours))
    total_discharge_kw: NDArray[np.floating[Any]] = np.zeros(len(hours))
    for c, d in zip(actual_charge_kw, actual_discharge_kw, strict=True):
        total_charge_kw = total_charge_kw + c
        total_discharge_kw = total_discharge_kw + d
    net_needed = load_kw + total_charge_kw - total_discharge_kw - solar_kw
    return np.maximum(0.0, -net_needed)


def _widen_export_pin_to_achieved(
    grid: GridConfig,
    *,
    hours: NDArray[np.float64],
    load_kw: NDArray[np.float64],
    solar_kw: NDArray[np.float64],
    actual_charge_kw: list[NDArray[np.float64]],
    actual_discharge_kw: list[NDArray[np.float64]],
) -> GridConfig:
    """The oracle's grid config with any P2P export pin relaxed into a
    band that contains what the day actually exported (nimbus issue
    #956).

    The SoC envelope is only half of the asymmetry. `fixed_export_kw`
    pins the oracle's export to **exactly** the committed rate in every
    committed period -- and a real day does not deliver a commitment to
    the watt. On the day this issue was filed from, the commitment was
    12.0 kW and the meter recorded 12.39-12.77 kW across 17:00-24:00, so
    the achieved trajectory was outside the oracle's feasible set in
    seven consecutive hours on top of being outside it on SoC.

    So the pin becomes a band, `[min(pin, achieved), max(pin, achieved)]`
    per period -- **exactly as wide as that day's own deviation and no
    wider, in whichever direction it went.**

    **v0.94.344 shipped this widening upward only, and that was wrong
    (nimbus issue #1001).** The stated reasoning was that an
    under-delivered commitment could not produce negative regret because
    "under-delivery makes achieved worse, not better" -- true whenever
    exporting into a P2P window is profitable, which is what a bonus rate
    is for. It is false when the committed export price is a fraction of
    the import price. Then the commitment is a **loss the oracle cannot
    decline**, and a household that quietly skipped it beats perfect
    foresight. Measured on a real install the day v0.94.344 shipped, and
    reduced to a minimal reproduction: a 12 kW commitment over seven
    hours at 7.5c export against 37c import gives `j_star` **$2.43 worse
    than doing nothing at all** and regret **-$4.95**.

    **The 2026-08-20 finding the pin exists for still survives**, because
    that finding is an upper bound -- its own guard is named
    `test_oracle_export_never_exceeds_the_real_fixed_rate_during_the_p2p_
    window`. The oracle still cannot chase a fictional market: the
    ceiling is the commitment, or what the day itself demonstrably
    exported, whichever is higher, and never `export_limit_kw`.

    **What dropping the floor does NOT do is lose the missed
    commitment.** That signal moves from being implicit -- inferable only
    by noticing regret had gone impossible -- to explicit, as
    `p2p_commitment_shortfall_kwh` on the report. Two different questions
    were being smuggled into one number, and separating them reports the
    shortfall more visibly than the infeasible oracle ever did.

    A day that delivers its commitment exactly -- every well-behaved day
    -- produces `lo == hi == pin` and is byte-identical to before this
    existed, as is any install with no P2P commitment configured at all.
    """
    if grid.fixed_export_kw is None:
        return grid

    achieved_export_kw = _achieved_grid_export_kw(
        hours=hours,
        load_kw=load_kw,
        solar_kw=solar_kw,
        actual_charge_kw=actual_charge_kw,
        actual_discharge_kw=actual_discharge_kw,
    )

    pin = np.asarray(grid.fixed_export_kw, dtype=np.float64)
    pinned = ~np.isnan(pin)
    if not pinned.any():
        return grid

    # A band is only ever created where a pin exists; everywhere else
    # stays NaN, which grid_export_bounds() reads as "no band".
    export_limit_arr = np.broadcast_to(
        np.asarray(grid.export_limit_kw, dtype=np.float64), pin.shape
    )
    lo = pin.copy()
    hi = np.full_like(pin, np.nan)
    # The physical/contracted export limit still wins over a
    # reconstruction: a measured export above it is sensor or unit
    # trouble, not a rate the oracle should be asked to match.
    hi[pinned] = np.minimum(
        np.maximum(pin[pinned], achieved_export_kw[pinned]), export_limit_arr[pinned]
    )
    # Never negative: GridConfig rejects a pin below 0, and a
    # reconstruction cannot export less than nothing.
    lo[pinned] = np.maximum(0.0, np.minimum(pin[pinned], achieved_export_kw[pinned]))
    # Reconstruction noise can leave hi a hair under lo once the export
    # limit clamp lands; the band must never invert.
    lo[pinned] = np.minimum(lo[pinned], hi[pinned])
    return replace(grid, fixed_export_kw=lo, fixed_export_max_kw=hi)


def _p2p_commitment_shortfall_kwh(
    grid: GridConfig,
    *,
    hours: NDArray[np.float64],
    achieved_export_kw: NDArray[np.floating[Any]],
) -> float:
    """Energy a committed P2P block asked for and the day did not deliver
    (nimbus issue #1001).

    The oracle no longer refuses to consider an under-delivered hour --
    it cannot, without making the comparison impossible -- so the missed
    commitment needs somewhere honest to live. This is it: summed over
    every committed period, `max(0, committed - delivered) * hours`.

    0.0 means either no P2P commitment is configured or every committed
    hour was met, which are usefully the same answer to "did the day
    honour its obligations".
    """
    if grid.fixed_export_kw is None:
        return 0.0
    pin = np.asarray(grid.fixed_export_kw, dtype=np.float64)
    pinned = ~np.isnan(pin)
    if not pinned.any():
        return 0.0
    shortfall_kw = np.maximum(0.0, pin[pinned] - achieved_export_kw[pinned])
    return float(np.sum(shortfall_kw * hours[pinned]))


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
    # nimbus #768/#585 (Mark Purcell): was a single `battery:
    # BatteryConfig` -- now the real fleet (home + any configured EV
    # `battery_participant`s), matching build_plan()'s own multi-battery
    # shape (nimbus #467) and oracle_dispatch()'s own extension (#768
    # Option 1). A single-element list is byte-identical to the old
    # single-battery behaviour -- see this module's own test suite for
    # the backward-compat proof. `commanded_charge_kw`/`commanded_
    # discharge_kw`/`actual_charge_kw`/`actual_discharge_kw`/`final_
    # soc_kwh_actual` below are now one entry per battery, in the SAME
    # order as `batteries` -- callers building these from real recorder
    # history (solver_writer.py's `_compute_report_for_window()`) match
    # each participant's own configured power/SoC sensor to its own
    # list index.
    batteries: list[BatteryConfig],
    solar: SolarConfig,
    load: LoadConfig,
    timestamps: list,
    # The REAL, settled P2P revenue for this exact day (ground truth,
    # not modeled) -- see module docstring.
    real_p2p_dollars_earned: float,
    commanded_charge_kw: list[NDArray[np.float64]],
    commanded_discharge_kw: list[NDArray[np.float64]],
    actual_charge_kw: list[NDArray[np.float64]],
    actual_discharge_kw: list[NDArray[np.float64]],
    final_soc_kwh_actual: list[float],
) -> QualityReport:
    hours = periods.hours
    n = len(hours)
    zero = np.zeros(n)
    zero_per_battery = [zero] * len(batteries)

    # Score J_ref/J_ach/J_star with NO terminal-value credit for leftover
    # battery energy (2026-09-06, Mark Purcell: "EPR is inconsistent
    # between charts and isn't being calculated correctly" -- his live
    # install showed a negative EPR that traced back to exactly this).
    #
    # This is the SAME distortion already found and fixed once before, in
    # the sibling 116KAT-HA-AI repo's own separate daily quality-writer
    # script (2026-08-29): crediting SoC left in the battery at the close
    # of an ALREADY-ELAPSED day with a forward-looking $/kWh value is a
    # guess about tomorrow's own prices this report has no honest basis
    # for making -- tomorrow's own report prices whatever actually carries
    # forward, using tomorrow's own real initial_soc_kwh. That fix zeroed
    # salvage_value/terminal_value_breakpoints for its own three
    # trajectories; this function -- the canonical, live-service-exposed
    # scorer the "Dispatch Regret" card calls into -- never received the
    # same fix, so it kept crediting terminal value on all three of
    # J_ref/J_ach/J_star (via evaluate_realized_cost() AND via J_star's own
    # oracle build_plan() call, whose LP objective includes it too). Both
    # scorers were internally self-consistent on their own, just
    # inconsistent WITH EACH OTHER for the identical real day -- exactly
    # matching "EPR is inconsistent between charts."
    #
    # Using dataclasses.replace() rather than mutating `batteries` itself:
    # only the three terminal-value fields are stripped for the scoring
    # calls below, PER BATTERY; every other field (efficiencies,
    # capacity, min_soc_kwh, charge/discharge cost, degradation cost)
    # stays each participant's own real, live config, since those
    # describe real physical/economic properties of that battery, not a
    # forward-looking valuation choice.
    battery_scoring = [
        replace(
            b, salvage_value=0.0, headroom_value=0.0, terminal_value_breakpoints=None
        )
        for b in batteries
    ]

    # nimbus issue #956 (household decision 2026-09-16, "go with B"): the
    # ORACLE alone is re-bounded so the achieved trajectory lies inside
    # its feasible set. Deliberately a separate list rather than folding
    # this into `battery_scoring` above: J_ref and J_ach price an
    # already-fixed trajectory and must be provably untouched by this
    # change, which a shared list would leave resting on the evaluator's
    # current internals instead of on construction.
    battery_oracle = [
        replace(b, min_soc_kwh=floor, max_soc_kwh=ceiling)
        for b, floor, ceiling in (
            (b, *_soc_envelope_containing_achieved(b, c, d, hours))
            for b, c, d in zip(
                battery_scoring, actual_charge_kw, actual_discharge_kw, strict=True
            )
        )
    ]

    # J_ref (idle -- no battery does anything): mathematically
    # independent of how many batteries are in the fleet, since every
    # committed charge/discharge array is zero regardless of count (the
    # charge_cost/discharge_cost terms are all multiplied by zero, and
    # terminal value is stripped above) -- still routed through the same
    # real multi-battery evaluator for a single, consistent code path
    # with j_ach/oracle_residual below, not a special-cased shortcut.
    j_ref_result = evaluate_realized_cost_multi(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        batteries=battery_scoring,
        charge_committed_kw=zero_per_battery,
        discharge_committed_kw=zero_per_battery,
        final_soc_kwh=[b.initial_soc_kwh for b in battery_scoring],
        # nimbus issue #1015: j_ref exports solar surplus like any other
        # trajectory, and on a day where that surplus lands inside a
        # committed P2P window the household would genuinely have earned
        # the premium on it. Pricing it at plain spot understates the
        # do-nothing baseline, which inflates BOTH sides of
        # EPR = (j_ref - j_ach) / (j_ref - j_star) and flatters the
        # Solver.
        #
        # `grid_oracle` is the right source: it is the same GridConfig
        # the oracle's own build_plan() reads the bonus from, so the two
        # counterfactuals are now priced by one model rather than two.
        # j_ach deliberately keeps its real settled figure instead --
        # see evaluate_realized_cost_multi()'s own docstring.
        export_bonus_grid=grid_oracle,
        period_starts=periods.period_starts,
    )
    j_ref = j_ref_result.total_cost

    j_ach_residual = evaluate_realized_cost_multi(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        batteries=battery_scoring,
        charge_committed_kw=actual_charge_kw,
        discharge_committed_kw=actual_discharge_kw,
        final_soc_kwh=final_soc_kwh_actual,
    )
    j_ach = j_ach_residual.total_cost - real_p2p_dollars_earned

    # nimbus issue #586 (Mark Purcell, real repro: 8 Sep started at 0.0%
    # against a 13% floor, #571, with 20.2c/kWh live at midnight and the
    # day's own real cheapest power at 4.9c/kWh six hours later at
    # 14:00). build_plan()'s own auto-derived soft_soc_penalty_per_kwh
    # (network.py) is deliberately steep -- DEFAULT_SOFT_SOC_PENALTY_
    # MULTIPLIER times the day's own MAXIMUM real price -- so the LIVE
    # solver always finds it worth recovering a below-floor SoC promptly.
    # That reasoning is backwards for a RETROSPECTIVE oracle: perfect
    # foresight has no reason to value promptness, only genuine price
    # advantage.
    #
    # A first attempt at this fix tried deriving the penalty from the
    # day's own MINIMUM price instead of its maximum, still applied on
    # every period -- proven wrong by this issue's own regression test
    # before shipping: the penalty accrues PER PERIOD for every period
    # spent below floor, so even a much smaller per-kWh rate still adds
    # up to far more than the one-time cost of an expensive-but-prompt
    # recovery once enough hours separate `now` from the day's own cheap
    # window (14 hours x 5.2 kWh x a "low" $0.50/kWh rate is still ~$36,
    # dwarfing the ~$1.05 one-time cost of just paying 20.2c/kWh at
    # midnight) -- so a lower recurring rate alone never actually removes
    # the urgency, it only raises the bar for how many hours of delay it
    # takes to reproduce the same rush.
    #
    # Real fix: the penalty stays exactly as auto-derived (untouched,
    # zero behaviour change) for the ordinary case -- a battery that
    # starts the day AT OR ABOVE its own floor never needed this
    # question answered at all, and the floor stays a real, fully-priced
    # preference throughout the plan, same as the live solver. Only when
    # the day's own STARTING condition is already below floor (a fact
    # about history the oracle had no way to have prevented, not a
    # choice within the scored window) is the penalty relaxed to zero for
    # this one oracle solve -- removing the false "must recover
    # immediately" pressure precisely in the one case #586 describes,
    # without weakening the floor's real economic weight on every other,
    # well-behaved day.
    # nimbus #768/#585: extended to the whole fleet -- relax to zero if
    # ANY participant's own real starting SoC is already below its own
    # floor (not just the home battery), same #586 reasoning applied per
    # battery: a retrospective, perfect-foresight oracle has no honest
    # reason to rush ANY participant's recovery, and one battery's own
    # pre-existing below-floor start is never a choice this scored
    # window could have prevented.
    oracle_soft_soc_penalty_per_kwh = (
        0.0 if any(b.initial_soc_kwh < b.min_soc_kwh for b in battery_scoring) else None
    )
    # nimbus issue #956, the second half of the same asymmetry: a P2P
    # commitment pins the oracle's export to EXACTLY the committed rate,
    # and a real day does not deliver to the watt. See
    # `_export_band_containing_achieved()` for the full reasoning.
    grid_oracle_scored = _widen_export_pin_to_achieved(
        grid_oracle,
        hours=hours,
        load_kw=load.forecast_kw,
        solar_kw=solar.forecast_kw,
        actual_charge_kw=actual_charge_kw,
        actual_discharge_kw=actual_discharge_kw,
    )
    # nimbus issue #1001: measured against the ORIGINAL commitment, not
    # the widened band -- the band is what the oracle is scored under,
    # the commitment is what the household actually owed.
    p2p_commitment_shortfall_kwh = _p2p_commitment_shortfall_kwh(
        grid_oracle,
        hours=hours,
        achieved_export_kw=_achieved_grid_export_kw(
            hours=hours,
            load_kw=load.forecast_kw,
            solar_kw=solar.forecast_kw,
            actual_charge_kw=actual_charge_kw,
            actual_discharge_kw=actual_discharge_kw,
        ),
    )
    oracle_plan = build_plan(
        periods=periods,
        grid=grid_oracle_scored,
        batteries=battery_oracle,
        solar=solar,
        loads=[load],
        soft_soc_penalty_per_kwh=oracle_soft_soc_penalty_per_kwh,
    )
    if not oracle_plan.is_optimal:
        msg = f"Oracle solve failed (status={oracle_plan.status}) -- should not happen with real, already-realized data unless genuinely infeasible"
        raise RuntimeError(msg)
    # mypy issue #384: Plan.total_cost is only ever None for a non-
    # optimal plan (network.py's own build_plan() only sets it from a
    # real result.objective on the optimal path) -- already guaranteed
    # by the is_optimal check just above, mypy just can't see through it.
    assert oracle_plan.total_cost is not None
    j_star = float(oracle_plan.total_cost)

    # oracle_plan.batteries is matched back to `battery_scoring` BY
    # NAME, not position -- build_plan() keys each participant by its
    # own `name` (nimbus #467), and this function makes no assumption
    # that the Plan preserves input list order.
    oracle_by_name = {bp.name: bp for bp in oracle_plan.batteries}
    oracle_charge_kw = [oracle_by_name[b.name].charge_kw for b in battery_scoring]
    oracle_discharge_kw = [oracle_by_name[b.name].discharge_kw for b in battery_scoring]
    oracle_final_soc_kwh = [
        float(oracle_by_name[b.name].soc_kwh[-1]) for b in battery_scoring
    ]

    # Oracle's own per-period cost, for the hourly breakdown -- evaluated
    # the SAME residual-only way as j_ach_residual above (base rate,
    # zero bonus knowledge), for a fair, apples-to-apples HOURLY shape
    # comparison. This means the hourly dict's own sum will NOT equal
    # (j_ach - j_star) whenever real bonus revenue differs between the
    # two trajectories -- same honest, documented gap
    # hourly_regret_breakdown() itself already discloses for
    # salvage_value; stated here too rather than silently surprising a
    # caller who sums the dict and compares it to the headline EPR.
    oracle_residual = evaluate_realized_cost_multi(
        hours=hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid_residual.import_price,
        export_price_real=grid_residual.export_price,
        batteries=battery_scoring,
        charge_committed_kw=oracle_charge_kw,
        discharge_committed_kw=oracle_discharge_kw,
        final_soc_kwh=oracle_final_soc_kwh,
    )
    hourly_regret = hourly_regret_breakdown(
        timestamps=timestamps,
        actual_cost_per_period=j_ach_residual.cost_per_period,
        oracle_cost_per_period=oracle_residual.cost_per_period,
    )

    # nimbus #1081: `j_star` and `j_ach` are produced by two DIFFERENT
    # computation paths, and nothing has ever checked they agree.
    #
    #   j_ach  = evaluate_realized_cost_multi(achieved) - real settled $
    #   j_star = oracle_plan.total_cost                  <- the LP objective
    #
    # `j_star <= j_ach` is supposed to hold by construction because the
    # oracle's feasible set contains the achieved trajectory. But that
    # argument is about TRAJECTORIES, and it only carries over to the
    # NUMBERS if both are priced by the same arithmetic. An LP objective
    # carries terms an evaluator does not (soft-SoC penalty, slack
    # penalties, the bonus as a chosen variable rather than an allocated
    # credit), so a disagreement between the paths can manufacture a
    # violation out of two individually-correct answers.
    #
    # This reprices the oracle's own plan through the SAME path j_ach
    # takes -- residual evaluator, minus its bonus credit -- so the two
    # can be compared directly. `j_star_path_delta` near zero means the
    # paths agree and a negative regret is a real modelling problem;
    # materially nonzero means the comparison itself is the problem, and
    # says by how much. Measured rather than argued, which is the whole
    # point: the leading explanation for the $0.02 on 15 Sep 2026 was
    # "probably LP tolerance", and this is what can actually confirm or
    # kill that.
    oracle_export_kw = _achieved_grid_export_kw(
        hours=hours,
        load_kw=load.forecast_kw,
        solar_kw=solar.forecast_kw,
        actual_charge_kw=oracle_charge_kw,
        actual_discharge_kw=oracle_discharge_kw,
    )
    j_star_evaluator = oracle_residual.total_cost - realized_export_bonus_credit(
        # Explicit dtype: the chained float64 +/- ops inside
        # _achieved_grid_export_kw() widen to floating[Any] under the numpy
        # stubs (the same note the SoC reconstructions below carry), which
        # realized_export_bonus_credit()'s own float64 signature rejects.
        grid_export_kw=oracle_export_kw.astype(np.float64),
        hours=hours,
        grid=grid_oracle_scored,
        period_starts=timestamps,
    )
    j_star_path_delta = j_star - j_star_evaluator

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
    # nimbus #768/#585: every per-trajectory reconstruction below is now
    # a FLEET AGGREGATE (summed net kW, summed/blended SoC) across every
    # battery in `batteries` -- see QualityReport's own docstring for the
    # exact aggregation rule. A single-battery install (still the common
    # case) gets a byte-identical result, since summing one element is a
    # no-op.
    j_ref_battery_net_kw = zero  # idle trajectory: no battery does anything
    # mypy issue #384: same numpy-stub dtype-widening note as above.
    # Explicit ndarray `start=` (see commanded_net_kw/actual_net_kw
    # below for the full reasoning) keeps this genuinely
    # NDArray[float64] rather than a `ndarray | Literal[0]` union.
    j_ach_battery_net_kw = sum(
        (
            (c - d).astype(np.float64)
            for c, d in zip(actual_charge_kw, actual_discharge_kw, strict=True)
        ),
        start=np.zeros(n, dtype=np.float64),
    )
    j_star_battery_net_kw = sum(
        (
            (c - d).astype(np.float64)
            for c, d in zip(oracle_charge_kw, oracle_discharge_kw, strict=True)
        ),
        start=np.zeros(n, dtype=np.float64),
    )
    # SoC per trajectory: j_ref flat; j_ach as measured (approximated by
    # integrating each battery's own actual net kW from its own initial
    # SoC using its own sqrt-split efficiencies, then summed); j_star
    # from the oracle plan directly (also summed across participants).
    total_initial_soc_kwh = sum(b.initial_soc_kwh for b in batteries)
    total_capacity_kwh = sum(b.capacity_kwh for b in batteries)
    # Actual per-period delta_kwh = charge * eta_c * dt - discharge * dt / eta_d,
    # per battery, then summed to a fleet-total SoC trajectory.
    dt = hours
    j_ach_soc_kwh = zero.copy()
    for b, c, d in zip(batteries, actual_charge_kw, actual_discharge_kw, strict=True):
        ach_delta = c * b.charge_efficiency * dt - d * dt / b.discharge_efficiency
        # mypy issue #384: numpy widens a python-float + ndarray[float64]
        # sum to floating[Any] in its own stubs -- a real stub-precision
        # gap, not a real bug (this project's own CLAUDE.md GBRT-vs-k-NN
        # finding documents the general pattern). Explicit dtype keeps
        # this array (and everything downstream that reads it) at the
        # real, narrower float64 contract the rest of this module
        # declares.
        j_ach_soc_kwh = j_ach_soc_kwh + (
            b.initial_soc_kwh + np.cumsum(ach_delta)
        ).astype(np.float64)
    j_star_soc_kwh = np.zeros(n, dtype=np.float64)
    for b in batteries:
        j_star_soc_kwh = j_star_soc_kwh + np.asarray(
            oracle_by_name[b.name].soc_kwh, dtype=np.float64
        )

    # SoC arrays as % (0..100) for consumer readability -- fleet-blended
    # (total stored kWh / total capacity kWh) whenever there's more than
    # one battery. Capacity 0 => no battery configured, keep the array
    # at 0.0 rather than dividing.
    def _soc_pct(soc_kwh: NDArray[np.float64]) -> NDArray[np.float64]:
        if total_capacity_kwh <= 0.0:
            return np.zeros(n)
        # mypy issue #384: same numpy-stub dtype-widening note as
        # j_ach_soc_kwh above.
        return (soc_kwh / total_capacity_kwh * 100.0).astype(np.float64)

    j_ref_soc_pct = np.full(n, _soc_pct(np.array([total_initial_soc_kwh]))[0])
    j_ach_soc_pct = _soc_pct(j_ach_soc_kwh)
    j_star_soc_pct = _soc_pct(j_star_soc_kwh)
    # Grid_kw derived from the reconstruction identity, per trajectory.
    # For j_star the LP uses MODEL solar/load (solar.forecast_kw /
    # load.forecast_kw) -- same inputs as j_ref/j_ach here because in
    # compute_daily_quality_report()'s calling site both are set from
    # yesterday's REAL measured history, but callers who pass a genuine
    # forecast for j_star will get the LP's own view of grid_kw.
    # mypy issue #384: same numpy-stub dtype-widening note as
    # j_ach_soc_kwh above -- chained float64 +/- ops widen to
    # floating[Any] in numpy's own stubs.
    j_ref_grid_kw = (
        load.forecast_kw - solar.forecast_kw + zero
    ).astype(  # idle battery
        np.float64
    )
    j_ach_grid_kw = (
        load.forecast_kw - solar.forecast_kw + j_ach_battery_net_kw
    ).astype(np.float64)
    j_star_grid_kw = (
        load.forecast_kw - solar.forecast_kw + j_star_battery_net_kw
    ).astype(np.float64)
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

    # mypy issue #384: same numpy-stub dtype-widening note as above.
    # Fleet-aggregate tracking too -- summed across every battery, same
    # reasoning as the reconstruction dicts above. Explicit ndarray
    # `start=` (rather than relying on sum()'s own default int 0) keeps
    # the return type genuinely NDArray[float64] even in the
    # (unreachable in practice -- "home" is always present, but not
    # something mypy can know) zero-battery case, instead of a
    # `ndarray | Literal[0]` union that every downstream consumer below
    # (both _hourly_means_by_key dict entries and compute_tracking_
    # fidelity/tracking_error_cost's own typed parameters) would then
    # have to defensively re-narrow.
    commanded_net_kw = sum(
        (
            (d - c).astype(np.float64)
            for c, d in zip(commanded_charge_kw, commanded_discharge_kw, strict=True)
        ),
        start=np.zeros(n, dtype=np.float64),
    )
    actual_net_kw = sum(
        (
            (d - c).astype(np.float64)
            for c, d in zip(actual_charge_kw, actual_discharge_kw, strict=True)
        ),
        start=np.zeros(n, dtype=np.float64),
    )
    tracking_result = compute_tracking_fidelity(
        hours=hours,
        commanded_kw=commanded_net_kw,
        actual_kw=actual_net_kw,
    )
    tracking_cost = tracking_error_cost(
        hours=hours,
        commanded_kw=commanded_net_kw,
        actual_kw=actual_net_kw,
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
        p2p_commitment_shortfall_kwh=round(p2p_commitment_shortfall_kwh, 4),
        j_star_evaluator=round(j_star_evaluator, 4),
        j_star_path_delta=round(j_star_path_delta, 4),
    )
