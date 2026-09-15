"""One-step-ahead LOAD nowcast skill -- "is Nimbus's ML load forecaster
actually beating naive persistence on this household's own data?"

nimbus issue #919. `forecast_regret.py` has answered the underlying
question since #273, but only ever driven by `reference_benchmark.py`
against synthetic scenarios -- no deployed install has ever computed it,
on any household, because the fifth input (the forecast *as it was made
at the time*) was never available after the fact.

The household's decision (2026-09-15) was to recover that input by
reading it back from Home Assistant recorder history rather than writing
new per-cycle storage. This module is the pure half of that: it takes an
already-reconstructed nowcast trail and turns it into real dollars.

## Which recorded sensor, and why it matters enormously

The obvious candidate -- `sensor.nimbus_household_load_total_forecast`'s
own state -- is **wrong, and silently so**. Its state is
`round(load_kw[0], 3)`, and `solver_inputs/load.py` overwrites
`load_kw[0]` with the live cross-check reading immediately before that
publish (the #429 anchor, which is correct and doing its job):

    live_load_kw = float(sw.ha_get(whole_house_cross_check_sensor)["state"])
    load_kw[0] = max(0.0, live_load_kw)

That sensor is the SAME one a quality report uses as its real-load
ground truth. So its recorded state is the ground truth echoed back, and
feeding it here would drive `j_forecast` onto `j_star` and publish
near-perfect skill on every install, forever. Confirmed live over three
hours of real recorded history: the two series agreed to the cent on
every single row, a few seconds apart.

It is not an edge case either -- a report needs that sensor configured
to run at all, and the anchor fires exactly when it is configured. The
precondition for computing the number was the precondition for it being
fake.

The right source is `whole_house_now_kw`, snapshotted BEFORE the anchor
precisely so #429's cross-check compares two genuine forecasts, and
already flattened to a real entity with recorded state:
`sensor.nimbus_solver_load_whole_house_cross_check_now_kw`. It is the
forecast **of the very sensor used as ground truth** -- forecast-of-X
against measured-X, same quantity, nothing to reconcile.

## Why solar is held at truth, deliberately

Nimbus publishes no solar *nowcast* state; solar forecasts come from
Solcast/Open-Meteo, i.e. a third party's skill, not this project's
forecaster. Including it would contaminate the number with someone
else's accuracy. So every scenario here is built on the REAL measured
solar, which makes `j_persistence - j_forecast` attributable to load
forecast quality alone. That is a sharper answer to #919's question, not
a compromise forced by what is available.

## What this is NOT

A nowcast trail measures **one-step-ahead** skill -- how well the
forecaster describes the moment it is in. It is NOT the day-ahead
forecast the dispatch was actually built on, which would require
persisting forecasts (the option the household ruled out). Both are real
questions; they are different questions. Everything published from this
module is named `load_nowcast_skill_*` so the horizon is on the face of
it and nobody compares it against `reference_benchmark.py`'s own
day-ahead figure by mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, PeriodGrid
from .forecast_regret import compute_forecast_regret

DEFAULT_MIN_COVERAGE = 0.5
"""Below this fraction of periods carrying a real recorded sample, the
result is None rather than a number.

The trail sensor updates roughly once every 3-4 minutes, against the
15-min grid a full-day window uses -- so healthy coverage is ~1.0 and
anything near this floor means real recorder gaps (a restart, a purge
boundary, an install that only just upgraded), not merely a sparse
sensor. Deliberately a floor on HONEST degradation, not a quality bar:
`resample_history_mean()` fills an empty period by holding the last
real sample, which is reasonable across a 4-minute gap and meaningless
across a six-hour one.
"""


@dataclass(frozen=True)
class LoadNowcastSkillResult:
    """One window's one-step-ahead load-forecast skill, in real dollars.

    The three J values are the same `RealizedCost.total_cost` figures
    `forecast_regret.py` produces, evaluated against the same real
    ground truth -- see that module for the three-scenario method. The
    only difference is that solar is held at its real measured values in
    every scenario here (see module docstring).
    """

    j_star: float
    """Perfect-foresight oracle -- the universal lower bound."""
    j_forecast: float
    """Plan built on the recovered nowcast trail, priced against reality."""
    j_persistence: float
    """Plan built on same-time-yesterday load, priced the same way."""
    coverage: float
    """Fraction of grid periods that carried at least one real recorded
    sample of the trail sensor. 1.0 means every period was measured."""
    n_periods_measured: int
    n_periods: int

    @property
    def value_add_dollars(self) -> float:
        """J_persistence - J_forecast. POSITIVE means the ML forecaster
        genuinely beat naive persistence over this window -- the one
        number a household can act on, for the same reason
        `forecast_regret.py` publishes the persistence delta rather than
        a bare regret figure ("regret is $4.20" has no comparison
        point; "$2.10 better than doing nothing smarter" has one).

        NEGATIVE is a real, reportable answer, not an error: it means
        persistence would have done better over this window. Publishing
        that honestly is the entire point of #919.
        """
        return self.j_persistence - self.j_forecast


def period_sample_coverage(
    sample_times: list[datetime],
    grid_times: list[datetime],
    period_hours: float,
) -> tuple[int, int]:
    """Count how many grid periods carry at least one real recorded
    sample, returning (measured, total).

    Needed because `resample_history_mean()` deliberately never reports
    a gap -- it falls back to the nearest-at-or-before sample so it
    "never fabricates a gap", which is right for reconstructing energy
    flow but leaves a caller unable to tell a measured period from a
    held-over one. This measures that separately rather than changing
    resampling behaviour every other report depends on.

    Uses the same half-open `[gt, gt + period_hours)` window as
    `resample_history_mean()` itself, so the two agree on exactly which
    samples belong to which period.
    """
    if not grid_times:
        return (0, 0)
    measured = 0
    for gt in grid_times:
        window_end = gt + timedelta(hours=period_hours)
        if any(gt <= t < window_end for t in sample_times):
            measured += 1
    return (measured, len(grid_times))


def compute_load_nowcast_skill(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar_real_kw: NDArray[np.float64],
    load_real_kw: NDArray[np.float64],
    load_nowcast_kw: NDArray[np.float64],
    load_persistence_kw: NDArray[np.float64],
    n_periods_measured: int,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> LoadNowcastSkillResult | None:
    """Score one window's one-step-ahead load-forecast skill.

    `load_nowcast_kw` is the recovered trail, already resampled onto the
    same grid as `load_real_kw` -- see the module docstring for which
    recorded sensor it must come from, and which one silently produces a
    fake result. `load_persistence_kw` is the recommended same-time-
    yesterday baseline: the real load sensor's own history shifted 24 h,
    which needs no new storage either.

    Returns None -- never a fabricated number -- when the window has too
    little real recorded history, when the arrays disagree on length, or
    when any scenario solve fails. A quality report that publishes
    nothing for a window is honest; one that publishes a confident
    figure built on held-over samples is not.

    Takes a single `battery` because `compute_forecast_regret()` does.
    On a multi-battery install this is the home battery and the resulting
    figure is that battery's, which is the right scope for a
    load-forecast-skill question -- an EV's own availability window is a
    separate variable (#467) and would confound it.
    """
    # No zero-period guard here deliberately: PeriodGrid.__post_init__()
    # already refuses an empty grid ("must have at least one period"), so
    # `n` cannot be 0 and the division below is safe. A second check here
    # would be unreachable code pretending to be a safety net.
    n = len(periods.hours)
    arrays = (solar_real_kw, load_real_kw, load_nowcast_kw, load_persistence_kw)
    if any(len(a) != n for a in arrays):
        return None

    coverage = n_periods_measured / n
    if coverage < min_coverage:
        return None

    try:
        regret = compute_forecast_regret(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_real_kw=solar_real_kw,
            load_real_kw=load_real_kw,
            # Solar held at truth in BOTH scenarios -- see module
            # docstring. This is what isolates load-forecast quality.
            solar_forecast_kw=solar_real_kw,
            solar_persistence_kw=solar_real_kw,
            load_forecast_kw=load_nowcast_kw,
            load_persistence_kw=load_persistence_kw,
        )
    except (RuntimeError, ValueError):
        # compute_forecast_regret() raises RuntimeError on a non-optimal
        # scenario solve. That is correct for the benchmark, which wants
        # to know loudly -- but this runs inside a live quality report on
        # every scored window, and one infeasible scenario must not take
        # the whole report down with it (the #366/#373 "degrade, never
        # wedge" discipline).
        return None

    return LoadNowcastSkillResult(
        j_star=regret.j_star,
        j_forecast=regret.j_forecast,
        j_persistence=regret.j_persistence,
        coverage=coverage,
        n_periods_measured=n_periods_measured,
        n_periods=n,
    )
