"""Training and prediction for Nimbus's load model.

Two model types, both pure-numpy (no scikit-learn -- confirmed live
2026-08-14 that it has no pre-built wheel for this Python version and
fails to build from source inside Home Assistant's own container, no C
compiler present at all):

  - k-NN (ml/model.py's original approach): a lazy learner, "find past
    moments that looked like this one, average what the load was then."
  - GBRT (ml/gbrt.py, added 2026-08-14): gradient-boosted regression
    trees, the same algorithm XGBoost/LightGBM implement, just without
    their compiled-code speed advantage -- not needed at this data scale.

Every retrain now VALIDATES both on real held-out (chronologically split,
never randomly -- a random split leaks future information into training
for a time series) data and picks whichever actually performs better for
THAT SPECIFIC load, rather than assuming one approach is universally
better. Confirmed via real backtesting (2026-08-14, 30 days of this
project's own live history, 4 different loads) that this matters: GBRT
won clearly with a full 30-day window on every load tested, but the
household's own real data showed real variation in the margin -- letting
each load's own retrain decide is more honest than hardcoding a winner
from one household's numbers.

Lag features (added the same session, same backtest): "what was this load
doing LAG_SHORT_STEPS/LAG_LONG_STEPS grid-steps ago" -- consistently among
the most important inputs for every load tested. At training time these
come straight from the same resampled history grid. At forecast time,
beyond the first couple of steps, no real "future" lag value exists yet --
predict() recursively feeds each step's own prediction back in as the lag
input for later steps (standard multi-step time-series forecasting
practice), then applies light exponential-smoothing (dampening) across the
output sequence so consecutive 15-minute steps don't jump unrealistically.

Model validation, extended further (2026-08-15): the knn-vs-gbrt
comparison above now also includes a seasonal-naive baseline ("what was
this load doing at this exact same time last week") as a genuine third
candidate, and reports MASE alongside raw MAE -- validation error scaled
by the load's own week-over-week variability, letting accuracy be
compared meaningfully across loads of very different magnitudes (a raw
kW MAE can't). GBRT's own fit() also gained early stopping (ml/gbrt.py),
used everywhere a real held-out validation set exists, so boosting stops
once it stops helping rather than always running the full fixed
n_estimators. Whichever model type wins can also get genuine model-
derived confidence bounds (two extra GBRT models predicting the low/high
quantile directly, not just a residual-based band) -- see
GBRT_QUANTILE_LOWER/UPPER and PredictionResult below; coordinator.py
falls back to calibrated_band()'s residual-based approach when these
aren't available (k-NN, or too little data to trust a quantile fit).

Every function in this module is blocking / CPU-bound and must always be
called from an executor thread (`hass.async_add_executor_job`), never
directly on Home Assistant's event loop -- see coordinator.py.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from ..const import LAG_LONG_STEPS, LAG_SHORT_STEPS, VALIDATION_HOLDOUT_FRACTION
from .features import build_features
from .gbrt import GBRT

_LOGGER = logging.getLogger(__name__)

# Number of nearest neighbors averaged per k-NN prediction. Small enough to
# stay local (real seasonal/time-of-day structure), large enough to smooth
# out noisy individual readings.
K_NEIGHBORS = 15
# Added to distances before inverting to weights, so an exact (or
# near-exact) match doesn't produce a division by ~zero.
DISTANCE_EPSILON = 1e-6

# Exponential-smoothing weight applied to the raw forecast sequence, most
# recent point weighted highest. 1.0 = no smoothing at all; lower values
# damp harder. 0.65 was picked to visibly smooth 15-minute-step jitter
# without flattening genuine ramps (e.g. a load turning on) into mush --
# a judgement call, not backtested precisely; worth revisiting with a real
# smoothness-vs-accuracy sweep later.
DAMPING_ALPHA = 0.65

# Seasonal-anchor blend weight (2026-08-16) -- weight on the RAW MODEL
# prediction for a seasonal-anchored step; (1 - this) on the
# seasonal_lookup value at that SAME timestamp (not lag-shifted, unlike
# the value used as a LAG INPUT elsewhere). Found live the morning after
# the seasonal_lookup fix itself shipped: even with a fully-populated,
# correctly-bucketed table (confirmed directly: 672/672 real entries),
# the model's own predicted OUTPUT still produced real, visible flat
# runs spanning up to several hours -- confirmed the lag INPUTS
# genuinely varied smoothly across that exact window while the model's
# output didn't. Root cause: GBRT (GBRT_N_ESTIMATORS/GBRT_MAX_DEPTH
# below) is a piecewise-constant function; with only 60 shallow trees,
# small real differences between consecutive 15-min seasonal_lookup
# values (a few tenths of a kW) don't reliably cross a tree's split
# threshold. Tested (not assumed) whether simply increasing model
# capacity fixes this: it does eliminate the flat runs, but triples the
# isolated-spike count (3->9->10->15 across a real sweep) with NO
# accuracy improvement -- trading one visible artifact for a worse one,
# exactly the "chaotic" outcome this project is explicitly building
# against. This blend instead targets ONLY the mechanism actually
# responsible (the model's own coarse output for anchored steps),
# leaving model capacity and every non-anchored (real-lag-driven)
# prediction completely untouched.
#
# 0.5 was chosen from a real sweep against real Whole House household
# data (weight 1.0/0.85/0.7/0.5/0.3/0.15/0.0), each scored on flat-run
# count, isolated-spike count, AND a genuine held-out accuracy check (a
# real day excluded from training, predicted with zero real recent lag
# so every step is seasonal-anchored, exactly the worst-case scenario
# this fix targets). Every weight from 0.85 down to 0.0 eliminated flat
# runs entirely; accuracy improved MONOTONICALLY as weight decreased
# toward 0 (the seasonal average alone outperforms the model's own
# far-future recursive output -- a real, sensible finding: the model's
# lag-driven signal degrades with recursion depth, the historical
# average doesn't). 0.5 was picked as the best BALANCE, not the single
# best metric: it also achieved the fewest isolated spikes of any
# weight tested (1, vs 3 at the current default of 1.0/no blend), while
# still meaningfully beating the un-blended baseline on accuracy
# (1.828 vs 1.850 MAE on the held-out day).
SEASONAL_BLEND_WEIGHT = 0.5

# GBRT hyperparameters. Deliberately shallow/few trees -- this trains once
# a day inside a HA executor thread on a few thousand rows, not a
# standalone training job; kept small enough that it stays comfortably
# fast at that scale rather than tuned for maximum accuracy regardless of
# runtime.
GBRT_N_ESTIMATORS = 60
GBRT_MAX_DEPTH = 3
GBRT_LEARNING_RATE = 0.1
GBRT_MIN_SAMPLES_LEAF = 5
# Early stopping (both the mean-regression AND quantile GBRT fits below)
# -- stop boosting once held-out validation error hasn't improved for
# this many consecutive rounds, rather than always running the full
# GBRT_N_ESTIMATORS. Set well below it so it actually gets a chance to
# trigger on a load whose signal saturates early. Verified 2026-08-15 via
# a standalone test (a trivial already-converged target correctly
# stopped at round 0; a real-signal target ran unaffected) before this
# was wired in here.
GBRT_EARLY_STOPPING_ROUNDS = 10

# Confidence-band calibration (added 2026-08-15, adapted from
# psweens/ml-forecast-lab's own documented approach: split conformal
# prediction with a rolling residual buffer -- keep real, recent
# absolute forecast errors and derive a coverage band from their
# empirical quantile, no extra model or training needed). Scoped down
# from their full per-lead-bucket cohort system: this implementation
# calibrates against real ONE-UPDATE-CYCLE-AHEAD residuals only
# (UPDATE_INTERVAL_MINUTES apart, not RESAMPLE_MINUTES -- coordinator.py
# resolves the previous cycle's near-term prediction against reality on
# every new cycle, regardless of the model's own coarser forecast-grid
# spacing) -- that's the shortest horizon coordinator.py can actually
# resolve every single update cycle -- then widens the band for longer
# leads via sqrt(1 + lead_hours), the standard approximation for how
# uncertainty accumulates over a horizon for a random-walk-like error
# process. Deliberately not claiming directly-calibrated accuracy at
# every possible lead time the way a full per-bucket system would.
CONFORMAL_COVERAGE = 0.8
MIN_RESIDUALS_FOR_CALIBRATION = 10
# Before enough real residuals exist to calibrate from, fall back to a
# band scaled off the point value itself rather than a falsely
# confident zero-width band -- matches the source repo's own
# "Calibrating..." cold-start concept.
COLD_START_BAND_FRACTION = 0.3
MAX_RESIDUALS_STORED = 200

# Genuine model-derived quantile bounds (2026-08-15) -- distinct from
# calibrated_band() above, which is residual-based and works for EITHER
# model type. When GBRT wins model selection AND there's a real
# validation set to early-stop against, two extra GBRT models are fit
# predicting the low/high quantile of the target directly (see
# ml/gbrt.py's own quantile-regression support) -- a genuinely different,
# model-derived source of uncertainty, not just "how wrong has this model
# been recently." Matches CONFORMAL_COVERAGE's own coverage target so
# both approaches mean the same thing ("80% of real outcomes should fall
# inside this band"). coordinator.py prefers these when available and
# falls back to calibrated_band() otherwise (k-NN, or too little
# validation data to trust a quantile fit) -- never mixes the two within
# one load's forecast.
GBRT_QUANTILE_LOWER = (1 - CONFORMAL_COVERAGE) / 2
GBRT_QUANTILE_UPPER = 1 - GBRT_QUANTILE_LOWER

# Seasonal-naive baseline / MASE scaling (2026-08-15) -- "what was this
# load doing at this exact same time, one full week ago." A week (not a
# day) deliberately captures weekday-vs-weekend patterns a same-day
# comparison alone would miss. Used two ways: as a trivial extra
# candidate in the validation_mae comparison (a real, non-ML sanity
# floor -- if a trained model can't beat "just look at last week," that's
# worth knowing), and as MASE's own scaling denominator (mean absolute
# week-over-week difference on the TRAINING portion) -- turns a raw kW
# MAE into a magnitude-independent ratio comparable across loads of very
# different sizes.
NAIVE_SEASONAL_STEPS_PER_WEEK_DAYS = 7
MIN_MASE_SCALE_POINTS = 20


@dataclass
class TrainedModel:
    """Training data plus normalization stats, plus (for GBRT) the fitted
    tree ensemble. Both model types keep the standardized training set --
    k-NN needs it for every prediction, GBRT keeps it only so a future
    retrain-comparison or fallback path doesn't need to refit from
    scratch to get it back.
    """

    model_type: str  # "gbrt" or "knn"
    x_mean: np.ndarray
    x_std: np.ndarray
    x_train: np.ndarray  # standardized feature rows, shape (n, n_features)
    y_train: np.ndarray  # matching load values, shape (n,)
    gbrt: GBRT | None
    trained_at: datetime
    training_points: int
    validation_mae: dict[str, float] = field(default_factory=dict)
    # Scale-independent counterpart to validation_mae -- see
    # NAIVE_SEASONAL_STEPS_PER_WEEK_DAYS comment above. Empty dict (not
    # populated with zeros/None) when there wasn't enough training-set
    # history to compute a trustworthy scale -- an absent key is an
    # honest "couldn't compute this," not a fabricated number.
    validation_mase: dict[str, float] = field(default_factory=dict)
    # Nimbus issue #113 (Mark Purcell, 2026-08-25): "if MASE is meant to
    # be computed, it isn't" -- validation_mase's own empty-dict-on-
    # insufficient-data behaviour (above) is correct by design, but
    # exposed no way to tell "genuinely can't compute this yet" apart
    # from "broken." Computing MASE's own scale needs a full CALENDAR
    # WEEK of lookback before even the FIRST training row, so any
    # install without ~8+ real days of history gets an empty dict every
    # time -- this field makes that visible instead of a bare {}: how
    # many real week-over-week points were actually found (out of
    # MIN_MASE_SCALE_POINTS needed), regardless of whether that count
    # crossed the threshold.
    mase_scale_points: int = 0
    # Same issue's second finding: "the dump should say what resolution
    # it trained at and what window it actually got, because '30 days'
    # is currently aspirational." resample_minutes is the fixed grid
    # spacing every training row is built on (RESAMPLE_MINUTES,
    # constant across every load/install); training_span_days is the
    # REAL elapsed calendar time between the first and last row that
    # actually survived lag-availability filtering into x_train/y_train
    # -- the true "how many days did this model actually train on"
    # answer, independent of both the configured train_days (which is a
    # request, not a guarantee) and training_points (a row count that
    # silently means something different at a different resample
    # resolution).
    resample_minutes: int = 0
    training_span_days: float = 0.0
    # Genuine model-derived quantile bounds -- None (not a GBRT with no
    # trees) when unavailable, so predict() and coordinator.py can tell
    # "no quantile model at all" apart from "a quantile model that
    # happens to output a small band."
    gbrt_lower: GBRT | None = None
    gbrt_upper: GBRT | None = None
    # Seasonal (weekday, hour) -> mean value lookup, built from the FULL
    # resampled training grid (every row with a real load value, no
    # lag-availability filtering -- unlike x_train/y_train). Deliberately
    # NOT keyed by minute-of-hour too (an earlier version of this fix
    # was, and it silently never matched anything -- see below). Added
    # 2026-08-15 to fix a real, confirmed-live exposure-bias bug in the
    # recursive multi-step forecast for power-signal targets: predict()'s
    # own lag_at() self-feeds its own predictions as later lag inputs once
    # past the first ~hour of real history (LAG_LONG_STEPS grid steps),
    # and for a signal whose lag features are heavily weighted (confirmed
    # by real backtesting) this compounds into a runaway anchor toward
    # whatever the recursive chain's OWN early trajectory happened to be
    # (e.g. "still mid-transition from an afternoon charge") rather than
    # ever reverting to the true, clean hour-of-day pattern. Confirmed
    # live 2026-08-15: forecasting Battery power from a real "charging
    # just stopped" moment, trained on real 45-day history, converged to
    # ~5.9-6.1kW evening peaks -- repeated near-identically day after
    # day -- against a real 45-day median of ~13kW every single evening
    # (tight IQR, ~12.4-14.0). Used by predict() (allow_negative=True
    # callers only -- see its own docstring) as the lag SOURCE once a
    # step's lag lookback falls past the real data horizon, instead of
    # the self-generated buffer -- grounds every later lag input in a
    # real, historically-observed value for that specific weekday/hour/
    # minute rather than letting error compound indefinitely. Loads
    # (allow_negative=False) are unaffected -- their own genuine near-
    # term momentum carry-over (e.g. "still running 15 min from now")
    # stays exactly as it was, only power signals with a strong, clean
    # calendar-driven rhythm and no real momentum-carry-over need get
    # anchored back to history like this. Bucketed by (weekday, hour,
    # 15-min-of-hour) -- e.g. minute=37 -> bucket 30. NOT exact minute:
    # both the training grid (built from whatever arbitrary wall-clock
    # instant the daily retrain job actually ran at) and the predict-time
    # grid (dt_util.utcnow() at whatever instant the coordinator's own
    # 2-min tick lands) step in exact 15-min increments from two
    # DIFFERENT, essentially-random starting offsets -- confirmed live
    # 2026-08-15 that keying by EXACT minute silently never matched
    # anything at all (a target's raw minute is almost never one of the
    # training grid's own 0/15/30/45 values), which is why the very first
    # version of this fix produced an UNCHANGED result despite compiling
    # and running without error. FLOORING to the nearest 15-min mark
    # (rather than matching the raw minute exactly) fixes this: any two
    # timestamps within the same 15-min window collapse to the identical
    # bucket regardless of their own arbitrary sub-15-min offset. An
    # intermediate version of this fix bucketed by (weekday, hour) only
    # (no minute-of-hour at all) to sidestep the exact-match bug the
    # simplest way possible -- that version worked, but gave every 15-min
    # step within the same hour an IDENTICAL lag input, and combined with
    # predict()'s damping-skip (needed to keep a genuine sharp transition
    # from blurring) produced a real, confirmed-live hard stair-step
    # (flat for up to 4 consecutive grid points, then an instant jump) in
    # the published forecast instead of a smoothly-varying curve. 15-min
    # bucketing keeps midnight (23:45 vs 00:00, still fully separate
    # buckets) exactly as sharp while removing that artificial flatness.
    seasonal_lookup: dict[tuple[int, int, int], float] = field(default_factory=dict)


def resample_last_value(
    events: list[tuple[datetime, float]], grid: list[datetime]
) -> list[float | None]:
    """Forward-fill a series of (timestamp, value) events onto a fixed `grid`.

    `events` must already be sorted ascending by timestamp. Returns None for
    any grid point before the first event (nothing to fill from yet).
    """
    times = [e[0] for e in events]
    values = [e[1] for e in events]
    out: list[float | None] = []
    for g in grid:
        idx = bisect_right(times, g) - 1
        out.append(values[idx] if idx >= 0 else None)
    return out


def _build_grid(
    start: datetime, end: datetime, resample_minutes: int
) -> list[datetime]:
    step = timedelta(minutes=resample_minutes)
    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += step
    return grid


def _knn_predict_batch(
    x_mean: np.ndarray,
    x_std: np.ndarray,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_query_raw: np.ndarray,
) -> np.ndarray:
    x_query_std = (x_query_raw - x_mean) / x_std
    k = min(K_NEIGHBORS, len(y_train))
    preds = np.empty(len(x_query_std))
    for i, row in enumerate(x_query_std):
        dists = np.sqrt(np.sum((x_train - row) ** 2, axis=1))
        nearest_idx = np.argpartition(dists, k - 1)[:k]
        nearest_dists = dists[nearest_idx]
        weights = 1.0 / (nearest_dists + DISTANCE_EPSILON)
        preds[i] = float(np.sum(weights * y_train[nearest_idx]) / np.sum(weights))
    return preds


def _mae(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a - b)))


def train_model(
    *,
    load_events: list[tuple[datetime, float]],
    temp_events: list[tuple[datetime, float]],
    humidity_events: list[tuple[datetime, float]],
    curtailment_events: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    resample_minutes: int,
    min_training_points: int,
    schedule_start_hour: float | None = None,
    schedule_end_hour: float | None = None,
    battery_events: list[tuple[datetime, float]] | None = None,
    grid_events: list[tuple[datetime, float]] | None = None,
    solar_events: list[tuple[datetime, float]] | None = None,
) -> TrainedModel | None:
    """Build a fresh model from real (local-time) history events.

    Trains AND validates both k-NN and GBRT on a chronological hold-out
    split, picks whichever actually performed better on THAT held-out
    data for THIS load, then refits the winner on the full dataset (the
    hold-out split is for model SELECTION only -- the deployed model
    trains on everything available, since more data is strictly better
    once the choice of model type is settled).

    Returns None (logging why) rather than raising, so a bad training
    cycle never takes the integration down -- the coordinator just keeps
    using whatever model it already has (or none yet) and tries again
    next cycle.
    """
    if not load_events:
        _LOGGER.warning("No load history available -- skipping this training cycle.")
        return None

    grid = _build_grid(start, end, resample_minutes)
    load_vals = resample_last_value(load_events, grid)

    # Seasonal lookup (see TrainedModel.seasonal_lookup's own docstring) --
    # built from EVERY grid point with a real observed value, deliberately
    # BEFORE the lag-availability-filtered x_rows loop below (which drops
    # the first LAG_LONG_STEPS rows and any row with a lag gap) -- this
    # table needs the widest possible real-data coverage, not just rows
    # that happened to also have clean lag history.
    # Bucketed by (weekday, hour, 15-min-of-hour) -- NOT just (weekday,
    # hour) -- confirmed live 2026-08-15, the SAME day the hourly version
    # shipped: an hourly bucket gives every 15-min step within that hour
    # an IDENTICAL lag input, and combined with the damping-skip fix
    # below (needed to keep a genuine sharp transition, like midnight,
    # from blurring), the model's raw output for seasonal-anchored steps
    # came out as a hard stair-step (flat for up to 4 consecutive grid
    # points, then an instant jump at the hour boundary) instead of a
    # smoothly-varying curve -- real live example: Whole House held
    # exactly 1.399 for 2.5 hours straight (16:18 through 18:48) then
    # jumped. Bucketing by 15-min-of-hour instead gives every grid point
    # its own real seasonal value, letting the model vary smoothly across
    # the hour the same way it already does for a genuine sharp boundary
    # like midnight -- 23:45 and 00:00 are STILL fully separate buckets,
    # so this does NOT reintroduce the original 45-min midnight-blur bug;
    # it only removes the artificial flatness WITHIN an hour, which was
    # never a deliberate design goal, just an unexamined side effect of
    # choosing hourly granularity to sidestep the earlier exact-minute
    # alignment bug (see the exact-minute mismatch note below).
    seasonal_sums: dict[tuple[int, int, int], list[float]] = {}
    hour_sums: dict[int, list[float]] = {}
    for g, lv in zip(grid, load_vals, strict=True):
        if lv is None:
            continue
        minute_bucket = (g.minute // 15) * 15
        seasonal_sums.setdefault((g.weekday(), g.hour, minute_bucket), []).append(lv)
        # Shrinkage target stays hour-only (not hour+minute_bucket) --
        # deliberately the BROADEST reasonably-robust reference available
        # (every weekday AND every 15-min sub-bucket within that hour),
        # since a 15-min-granularity bucket has the same thin ~6-7-sample
        # count as the old hourly one did and needs an equally robust
        # target to shrink toward, not a narrower one.
        hour_sums.setdefault(g.hour, []).append(lv)
    # Shrinkage toward the overall per-hour average (2026-08-15, found
    # live the same day the seasonal fix itself was built): a 45-day
    # window gives only ~6-7 real samples per individual (weekday, hour)
    # bucket -- confirmed live that one genuinely quiet/anomalous Sunday
    # alone was enough to drag that one weekday's own evening average
    # down to ~8kW against every other weekday's ~13kW, with nothing in
    # this household's real automation history suggesting the underlying
    # pattern is actually weekday-dependent (the P2P sell automation runs
    # the same way every night). SHRINKAGE_K=5 means a bucket with 5 real
    # samples gets weighted 50/50 with the far more robust all-weekday
    # hourly average; a bucket with many more samples relies mostly on
    # its own genuine value; a bucket with very few leans almost entirely
    # on the broader hourly pattern instead of trusting a handful of
    # points. Standard empirical-Bayes-style shrinkage, not a special
    # per-load hack.
    SHRINKAGE_K = 5
    seasonal_lookup: dict[tuple[int, int, int], float] = {}
    for (wd, hr, mb), vs in seasonal_sums.items():
        hour_mean = sum(hour_sums[hr]) / len(hour_sums[hr])
        n = len(vs)
        seasonal_lookup[(wd, hr, mb)] = (sum(vs) + SHRINKAGE_K * hour_mean) / (
            n + SHRINKAGE_K
        )
    temp_vals = (
        resample_last_value(temp_events, grid) if temp_events else [None] * len(grid)
    )
    humidity_vals = (
        resample_last_value(humidity_events, grid)
        if humidity_events
        else [None] * len(grid)
    )
    curtailment_vals = (
        resample_last_value(curtailment_events, grid)
        if curtailment_events
        else [None] * len(grid)
    )
    battery_vals = (
        resample_last_value(battery_events, grid)
        if battery_events
        else [None] * len(grid)
    )
    grid_vals = (
        resample_last_value(grid_events, grid) if grid_events else [None] * len(grid)
    )
    solar_vals = (
        resample_last_value(solar_events, grid) if solar_events else [None] * len(grid)
    )

    x_rows: list[list[float]] = []
    y_vals: list[float] = []
    # Parallel to x_rows/y_vals (same length, same order, some grid
    # indices skipped whenever a row was None-filtered above) -- kept so
    # the seasonal-naive/MASE code below can look up "this row's actual
    # position in `grid`/`load_vals`" and "this row's own lag_long value"
    # without re-deriving either from scratch or assuming row index i
    # lines up with grid index i (it doesn't, once any rows are skipped).
    grid_indices: list[int] = []
    lag_long_vals: list[float] = []
    # Start at LAG_LONG_STEPS so every row has real lag history behind it
    # from this same grid -- no separate fetch needed at training time,
    # unlike at forecast time where the lag has to come from somewhere.
    for i in range(LAG_LONG_STEPS, len(grid)):
        lv = load_vals[i]
        lag_short_v = load_vals[i - LAG_SHORT_STEPS]
        lag_long_v = load_vals[i - LAG_LONG_STEPS]
        if lv is None or lag_short_v is None or lag_long_v is None:
            continue
        tv = temp_vals[i] if temp_vals[i] is not None else 22.0
        hv = humidity_vals[i] if humidity_vals[i] is not None else 50.0
        cv = curtailment_vals[i] if curtailment_vals[i] is not None else 0.0
        bv = battery_vals[i] if battery_vals[i] is not None else 0.0
        gv = grid_vals[i] if grid_vals[i] is not None else 0.0
        sv = solar_vals[i] if solar_vals[i] is not None else 0.0
        x_rows.append(
            build_features(
                grid[i],
                tv,
                hv,
                lag_short_v,
                lag_long_v,
                cv,
                schedule_start_hour,
                schedule_end_hour,
                bv,
                gv,
                sv,
            )
        )
        y_vals.append(lv)
        grid_indices.append(i)
        lag_long_vals.append(lag_long_v)

    if len(x_rows) < min_training_points:
        _LOGGER.warning(
            "Only %d usable training points (need >= %d) -- skipping this cycle.",
            len(x_rows),
            min_training_points,
        )
        return None

    x_all = np.array(x_rows, dtype=np.float64)
    y_all = np.array(y_vals, dtype=np.float64)
    grid_idx_all = np.array(grid_indices, dtype=np.int64)
    lag_long_all = np.array(lag_long_vals, dtype=np.float64)
    week_steps = round(NAIVE_SEASONAL_STEPS_PER_WEEK_DAYS * 24 * 60 / resample_minutes)

    # Chronological split -- x_rows/y_vals are already in time order since
    # the grid itself is, so a plain index cut is a real chronological
    # split, not a random one.
    split = int(len(x_all) * (1 - VALIDATION_HOLDOUT_FRACTION))
    split = max(
        split, min_training_points // 2
    )  # keep a real training set even on a small window
    x_tr, y_tr = x_all[:split], y_all[:split]
    x_val, y_val = x_all[split:], y_all[split:]
    grid_idx_tr, grid_idx_val = grid_idx_all[:split], grid_idx_all[split:]
    lag_long_val = lag_long_all[split:]

    x_mean = x_tr.mean(axis=0)
    x_std = x_tr.std(axis=0)
    x_std[x_std < 1e-9] = 1.0  # avoid divide-by-zero on a constant feature column
    x_tr_std = (x_tr - x_mean) / x_std

    validation_mae: dict[str, float] = {}
    validation_mase: dict[str, float] = {}
    mase_scale_points = 0
    model_type = (
        "knn"  # safe default if validation set is too small to compare meaningfully
    )
    gbrt_lower_final: GBRT | None = None
    gbrt_upper_final: GBRT | None = None

    if len(x_val) >= 20:
        # Both candidates are fit on x_tr_std (standardized against the
        # TRAINING portion's own mean/std), so both must be evaluated on
        # x_val standardized the SAME way, not raw. k-NN's own helper
        # re-standardizes its query rows internally (it's called with raw
        # x_val on purpose, matching its normal calling convention
        # elsewhere in this module); GBRT's predict() has no such built-in
        # step -- its tree thresholds were learned directly against
        # standardized values, so an un-standardized x_val here would
        # silently evaluate every split against the wrong scale and make
        # GBRT look far worse than it really is. Caught in review before
        # this ever ran against real data.
        x_val_std = (x_val - x_mean) / x_std

        knn_val_pred = _knn_predict_batch(x_mean, x_std, x_tr_std, y_tr, x_val)
        validation_mae["knn"] = _mae(y_val, knn_val_pred)

        gbrt_val = GBRT(
            n_estimators=GBRT_N_ESTIMATORS,
            max_depth=GBRT_MAX_DEPTH,
            learning_rate=GBRT_LEARNING_RATE,
            min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
        )
        gbrt_val.fit(
            x_tr_std,
            y_tr,
            x_val=x_val_std,
            y_val=y_val,
            early_stopping_rounds=GBRT_EARLY_STOPPING_ROUNDS,
        )
        gbrt_val_pred = gbrt_val.predict(x_val_std)
        validation_mae["gbrt"] = _mae(y_val, gbrt_val_pred)

        # Seasonal-naive baseline: "what was this load doing at this exact
        # same time one week ago" -- a genuine, trivial-to-compute
        # non-ML candidate in the same comparison, not just a separately
        # reported number. Falls back to the row's own lag_long value
        # (the same fallback predict() itself uses) on any validation
        # row where a week-ago grid point doesn't exist yet or wasn't
        # observed -- honest best-available reference, not a skipped row
        # (skipping would silently shrink the comparison's sample size
        # relative to knn/gbrt's own full validation set).
        naive_val_pred = np.empty(len(grid_idx_val))
        for j, idx in enumerate(grid_idx_val):
            week_ago_idx = int(idx) - week_steps
            week_val = load_vals[week_ago_idx] if week_ago_idx >= 0 else None
            naive_val_pred[j] = week_val if week_val is not None else lag_long_val[j]
        validation_mae["naive"] = _mae(y_val, naive_val_pred)

        model_type = "gbrt" if validation_mae["gbrt"] < validation_mae["knn"] else "knn"
        _LOGGER.info(
            "Model validation: knn_mae=%.4f gbrt_mae=%.4f naive_mae=%.4f -> using %s",
            validation_mae["knn"],
            validation_mae["gbrt"],
            validation_mae["naive"],
            model_type,
        )

        # MASE: validation_mae scaled by the TRAINING set's own mean
        # absolute week-over-week difference -- turns a raw kW error into
        # a magnitude-independent ratio (MASE < 1.0 = model beats the
        # naive seasonal baseline; >= 1.0 = it doesn't), comparable across
        # loads of very different sizes in a way raw MAE never is. Scale
        # computed from TRAINING data only (never validation) since it's
        # meant to characterise the load's own inherent week-to-week
        # variability, not this particular validation split.
        mase_diffs: list[float] = []
        for idx, y_true in zip(grid_idx_tr.tolist(), y_tr.tolist(), strict=True):
            week_ago_idx = idx - week_steps
            week_val = load_vals[week_ago_idx] if week_ago_idx >= 0 else None
            if week_val is not None:
                mase_diffs.append(abs(y_true - week_val))
        # Recorded regardless of whether the threshold below is met --
        # see TrainedModel.mase_scale_points's own docstring (nimbus
        # issue #113): an honest count, not just a pass/fail bit.
        mase_scale_points = len(mase_diffs)
        if len(mase_diffs) >= MIN_MASE_SCALE_POINTS:
            mase_scale = float(np.mean(mase_diffs))
            if mase_scale > 1e-9:
                validation_mase = {k: v / mase_scale for k, v in validation_mae.items()}
                _LOGGER.info(
                    "Model validation (MASE, scale=%.4f): knn=%.3f gbrt=%.3f naive=%.3f",
                    mase_scale,
                    validation_mase["knn"],
                    validation_mase["gbrt"],
                    validation_mase["naive"],
                )

        # Genuine model-derived quantile bounds -- only when GBRT actually
        # won (fitting quantile models for a model type that isn't even
        # deployed would be wasted work) and there's a real validation
        # set to early-stop against (fitting without it risks an
        # overfit, artificially-narrow band with no way to catch it here).
        # Deliberately trained on x_tr_std/y_tr only (the SAME split used
        # for model selection above), not x_all -- letting them see the
        # validation rows they're also early-stopping against would be
        # real leakage. This means these two models see slightly less
        # data than gbrt_final below (which legitimately retrains on
        # everything once the model TYPE is settled) -- an honest
        # trade-off for genuinely being able to use early stopping.
        if model_type == "gbrt":
            gbrt_lower_final = GBRT(
                n_estimators=GBRT_N_ESTIMATORS,
                max_depth=GBRT_MAX_DEPTH,
                learning_rate=GBRT_LEARNING_RATE,
                min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
                quantile=GBRT_QUANTILE_LOWER,
            )
            gbrt_lower_final.fit(
                x_tr_std,
                y_tr,
                x_val=x_val_std,
                y_val=y_val,
                early_stopping_rounds=GBRT_EARLY_STOPPING_ROUNDS,
            )
            gbrt_upper_final = GBRT(
                n_estimators=GBRT_N_ESTIMATORS,
                max_depth=GBRT_MAX_DEPTH,
                learning_rate=GBRT_LEARNING_RATE,
                min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
                quantile=GBRT_QUANTILE_UPPER,
            )
            gbrt_upper_final.fit(
                x_tr_std,
                y_tr,
                x_val=x_val_std,
                y_val=y_val,
                early_stopping_rounds=GBRT_EARLY_STOPPING_ROUNDS,
            )
    else:
        _LOGGER.info(
            "Only %d validation points -- too few to compare models, defaulting to k-NN.",
            len(x_val),
        )

    # Refit the WINNING model type on everything (train + validation) --
    # the hold-out split above was for model selection only.
    x_mean_final = x_all.mean(axis=0)
    x_std_final = x_all.std(axis=0)
    x_std_final[x_std_final < 1e-9] = 1.0
    x_all_std = (x_all - x_mean_final) / x_std_final

    gbrt_final: GBRT | None = None
    if model_type == "gbrt":
        gbrt_final = GBRT(
            n_estimators=GBRT_N_ESTIMATORS,
            max_depth=GBRT_MAX_DEPTH,
            learning_rate=GBRT_LEARNING_RATE,
            min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
        )
        # No early stopping here, deliberately -- this refit trains on
        # x_all_std (train+val combined, once the model TYPE is already
        # settled), so there's no legitimate held-out set left to early-
        # stop against without leaking. Runs the full fixed
        # GBRT_N_ESTIMATORS, same as before this feature existed.
        gbrt_final.fit(x_all_std, y_all)

    # Real elapsed calendar span of the rows that actually survived
    # lag-availability filtering (grid_indices), not the full requested
    # [start, end) window -- see TrainedModel.training_span_days's own
    # docstring (nimbus issue #113).
    training_span_days = (
        (grid[grid_indices[-1]] - grid[grid_indices[0]]).total_seconds() / 86400.0
        if grid_indices
        else 0.0
    )

    _LOGGER.info("Trained %s model on %d points.", model_type, len(x_rows))
    return TrainedModel(
        model_type=model_type,
        x_mean=x_mean_final,
        x_std=x_std_final,
        x_train=x_all_std,
        y_train=y_all,
        gbrt=gbrt_final,
        trained_at=end,
        training_points=len(x_rows),
        validation_mae=validation_mae,
        validation_mase=validation_mase,
        mase_scale_points=mase_scale_points,
        resample_minutes=resample_minutes,
        training_span_days=round(training_span_days, 2),
        gbrt_lower=gbrt_lower_final,
        gbrt_upper=gbrt_upper_final,
        seasonal_lookup=seasonal_lookup,
    )


def calibrated_band(
    residuals: list[float], point_value: float, lead_hours: float
) -> float:
    """Half-width of the confidence band around `point_value` at
    `lead_hours` ahead, given a rolling buffer of real one-update-cycle-
    ahead absolute residuals (coordinator.py owns collecting/persisting
    these -- this function is a pure calculation, no I/O, no state).

    Returns 0.0 for a genuinely zero point_value with no residual data
    at all (e.g. a load that's never been observed running) -- a
    fraction-of-point-value fallback would also be zero in that case,
    which is honest: there's nothing to base a band on yet.
    """
    if len(residuals) < MIN_RESIDUALS_FOR_CALIBRATION:
        return point_value * COLD_START_BAND_FRACTION
    near_term_half_width = float(np.percentile(residuals, CONFORMAL_COVERAGE * 100))
    return near_term_half_width * float(np.sqrt(1 + max(0.0, lead_hours)))


@dataclass
class PredictionResult:
    """Output of predict(). `model_lower`/`model_upper` are populated only
    when the underlying model produced genuine model-derived quantile
    bounds (GBRT with quantile sub-models fitted, ML path only -- never
    the deterministic path, which has no uncertainty to express at all).
    None (not a list of zeros) otherwise, so coordinator.py can tell
    "no quantile model available" apart from "a quantile model that
    happens to output a zero-width band" and fall back to
    calibrated_band()'s residual-based bands in the former case only --
    the two sources are never meant to be mixed within one load's single
    forecast.
    """

    values: list[float]
    model_lower: list[float] | None = None
    model_upper: list[float] | None = None


def _in_schedule(hour_frac: float, start: float, end: float) -> bool:
    """Same wrap-aware window test as ml/features.py's own in_schedule
    computation -- duplicated rather than imported to keep this module's
    only dependency on features.py at build_features() itself, and because
    this one deliberately takes an already-computed hour_frac rather than
    a datetime (predict()'s deterministic path below calls it once per
    timestamp in a tight loop with no need to re-derive hour_frac each
    time from scratch).
    """
    if start <= end:
        return start <= hour_frac < end
    return hour_frac >= start or hour_frac < end


def predict(
    trained: TrainedModel,
    timestamps: list[datetime],
    temps: list[float],
    humidities: list[float],
    recent_load_values: list[tuple[datetime, float]],
    resample_minutes: int,
    curtailments: list[float] | None = None,
    schedule_start_hour: float | None = None,
    schedule_end_hour: float | None = None,
    expected_load_kw: float | None = None,
    batteries_kw: list[float] | None = None,
    grids_kw: list[float] | None = None,
    solars_kw: list[float] | None = None,
    allow_negative: bool = False,
    seasonal_anchor: bool = False,
) -> PredictionResult:
    """Predict load at each of `timestamps` (must be ascending, evenly
    spaced by `resample_minutes`), given matching `temps`/`humidities`/
    `curtailments`/`batteries_kw`/`grids_kw`/`solars_kw` (each one value
    per timestamp, already aligned by the caller; any may be omitted
    entirely for a load with that sensor unconfigured, in which case it's
    treated as 0.0 throughout). `curtailments` should come from the
    curtailment source's own forward *forecast* where available (some
    curtailment sources plan ahead) rather than a held-flat current value
    -- genuinely more informative when a real forecast exists.
    `batteries_kw`/`grids_kw`/`solars_kw` are REAL MEASURED values only
    (2026-08-15) -- these three have no forward-looking source of their
    own at all (unlike curtailment, which sometimes does), so the caller
    holds the current real reading flat across the whole horizon; see
    this repo's own CLAUDE.md PRIME DIRECTIVE for why this can never be
    an optimizer's own plan/forecast instead.

    `allow_negative` (2026-08-15, real bug found live): every point
    predicted by the ML path was being clamped to >= 0.0, correct for a
    load (physically can never draw negative power) but WRONG for a
    signed power-signal target like Battery (negative = charging) or
    Grid (negative = export) -- confirmed live: the real battery was
    charging at -29.9kW while Nimbus's own "right now" forecast showed
    exactly 0.0, because the model's own (correctly negative) raw
    prediction was being silently zeroed by this clamp on every single
    step. Defaults to False (unchanged behaviour for loads) -- callers
    forecasting a genuinely signed target must pass True.

    Deterministic override (2026-08-15): when `expected_load_kw` is given
    ALONGSIDE both schedule bounds, this load is in the user's explicit
    "I know exactly when this runs and exactly how much it draws" mode --
    skip the ML model entirely and return `expected_load_kw` inside the
    window, 0.0 outside it, for every timestamp. This is not a smarter
    guess; it's the literal rule the user configured. Confirmed live this
    matters for real loads with genuinely consistent rated power but
    inconsistent real-world on/off timing (manual top-ups, etc.) -- the ML
    path was averaging across that timing noise and blurring a true ~3.7kW
    load's forecast down to ~2kW, which no amount of model tuning fixes
    since the averaging is the correct behaviour for the ML path, just not
    the desired one for a load the user can fully characterise up front.

    Recursive multi-step forecast (ML path only): the first few steps can
    use REAL recent history (`recent_load_values`) for their lag features;
    once the horizon extends past that real history, each step's own
    just-made prediction becomes the lag input for later steps -- standard
    practice for lag-feature time-series forecasting, and the only option,
    since no real future data exists yet to use instead. This is also why
    this function predicts one step at a time rather than a single
    vectorized batch, unlike the old lag-free k-NN predict().

    A light exponential-smoothing pass (DAMPING_ALPHA) is applied to the
    raw ML output sequence before returning (deterministic path has no
    such pass -- there's nothing to smooth, it's already exact), so
    consecutive 15-minute steps don't jump unrealistically -- pure
    post-processing, doesn't feed back into the model itself. Model-
    derived quantile bounds (`PredictionResult.model_lower`/`model_upper`,
    2026-08-15), when available, are computed in RAW (pre-smoothing)
    space per step, then re-centered around the SMOOTHED point value
    (`smoothed[i] +/- raw_half_width[i]`) -- keeps the point curve's own
    smoothness while preserving the band's real, model-learned shape
    (typically widening with horizon) rather than smoothing the bounds
    independently and risking them drifting out of sync with the point
    estimate they're supposed to surround.
    """
    if (
        expected_load_kw is not None
        and schedule_start_hour is not None
        and schedule_end_hour is not None
    ):
        return PredictionResult(
            values=[
                expected_load_kw
                if _in_schedule(
                    ts.hour + ts.minute / 60.0, schedule_start_hour, schedule_end_hour
                )
                else 0.0
                for ts in timestamps
            ]
        )

    step = timedelta(minutes=resample_minutes)
    # Rolling buffer of (timestamp, value), seeded from real history, that
    # we append our own predictions onto as we go -- this IS the lag
    # source for every step.
    buffer = sorted(recent_load_values, key=lambda p: p[0])
    buffer_times = [p[0] for p in buffer]
    buffer_vals = [p[1] for p in buffer]
    default_lag = float(np.mean(trained.y_train)) if len(trained.y_train) else 0.0
    # Boundary past which lag_at() would otherwise be forced to use a
    # SELF-GENERATED prediction rather than a real observed value --
    # captured BEFORE the loop below starts appending its own predictions
    # onto buffer_times/buffer_vals. None (no real recent data at all)
    # means every lookup is beyond the cutoff. See TrainedModel.
    # seasonal_lookup's own docstring for why this matters.
    real_data_cutoff = buffer_times[-1] if buffer_times else None

    def lag_at(target: datetime) -> float:
        if seasonal_anchor and (real_data_cutoff is None or target > real_data_cutoff):
            # getattr(..., {}) not trained.seasonal_lookup directly --
            # plain @dataclass pickling restores an OLD persisted
            # TrainedModel's __dict__ verbatim, skipping __init__ and its
            # default_factory=dict entirely, so a .pkl saved under the
            # PREVIOUS code (before this field existed) unpickles with no
            # seasonal_lookup attribute at all. Caught before shipping,
            # not live -- would otherwise raise a real AttributeError on
            # the very first predict() call after this deploys, for
            # every load AND every signal (not just Battery), until each
            # one's next scheduled retrain replaces the stale pickle.
            target_bucket = (target.minute // 15) * 15
            seasonal_v = getattr(trained, "seasonal_lookup", {}).get(
                (target.weekday(), target.hour, target_bucket)
            )
            if seasonal_v is not None:
                return seasonal_v
            # No (weekday, hour) match for this particular bucket (e.g. a
            # training window that happened to never cover it) -- fall
            # through to the self-generated buffer below rather than
            # silently using the flat default_lag, which would be a
            # worse approximation than "whatever the chain currently
            # believes" for a single missing bucket.
        idx = bisect_right(buffer_times, target) - 1
        return buffer_vals[idx] if idx >= 0 else default_lag

    def seasonal_at(ts: datetime) -> float | None:
        """Same table, but keyed at TS ITSELF -- not lag-shifted like
        lag_at() above. Used only for SEASONAL_BLEND_WEIGHT blending
        (see that constant's own docstring for the full story): a
        step's LAG INPUT legitimately needs to look at an earlier
        point (that's what "lag" means), but this is asking a
        different question -- "what does history say THIS exact point
        itself should look like" -- to damp the model's own coarse
        tree-output artifact for seasonal-anchored steps.
        """
        mb = (ts.minute // 15) * 15
        return getattr(trained, "seasonal_lookup", {}).get((ts.weekday(), ts.hour, mb))

    curtailment_list = (
        curtailments if curtailments is not None else [0.0] * len(timestamps)
    )
    battery_list = batteries_kw if batteries_kw is not None else [0.0] * len(timestamps)
    grid_list = grids_kw if grids_kw is not None else [0.0] * len(timestamps)
    solar_list = solars_kw if solars_kw is not None else [0.0] * len(timestamps)
    has_quantile_models = (
        trained.gbrt_lower is not None and trained.gbrt_upper is not None
    )

    raw_preds: list[float] = []
    raw_half_widths: list[float] = []
    # See the DAMPING_ALPHA-skip comment below, right before this list is
    # used -- tracked per-step here (not recomputed there) so it reflects
    # EXACTLY the same condition lag_at() itself used for this step's
    # lag_long lookup, not a second, independently-derived guess.
    seasonal_anchored: list[bool] = []
    for ts, temp, humidity, curtailment, battery_kw, grid_kw, solar_kw in zip(
        timestamps,
        temps,
        humidities,
        curtailment_list,
        battery_list,
        grid_list,
        solar_list,
        strict=True,
    ):
        lag_short_t = ts - LAG_SHORT_STEPS * step
        lag_long_t = ts - LAG_LONG_STEPS * step
        lag_short_v = lag_at(lag_short_t)
        lag_long_v = lag_at(lag_long_t)
        seasonal_anchored.append(
            seasonal_anchor
            and (real_data_cutoff is None or lag_long_t > real_data_cutoff)
        )

        x_row = np.array(
            [
                build_features(
                    ts,
                    temp,
                    humidity,
                    lag_short_v,
                    lag_long_v,
                    curtailment,
                    schedule_start_hour,
                    schedule_end_hour,
                    battery_kw,
                    grid_kw,
                    solar_kw,
                )
            ],
            dtype=np.float64,
        )
        x_row_std = (x_row - trained.x_mean) / trained.x_std

        if trained.model_type == "gbrt" and trained.gbrt is not None:
            pred = float(trained.gbrt.predict(x_row_std)[0])
        else:
            pred = float(
                _knn_predict_batch(
                    trained.x_mean,
                    trained.x_std,
                    trained.x_train,
                    trained.y_train,
                    x_row,
                )[0]
            )

        # Seasonal blend (see SEASONAL_BLEND_WEIGHT's own docstring for
        # the full story) -- only for steps already seasonal-anchored,
        # only when a real entry exists for THIS exact timestamp (a
        # missing bucket, same as lag_at()'s own handling, means trust
        # the raw model prediction rather than silently skip blending
        # with nothing).
        if seasonal_anchored[-1]:
            seasonal_v = seasonal_at(ts)
            if seasonal_v is not None:
                pred = (
                    SEASONAL_BLEND_WEIGHT * pred
                    + (1 - SEASONAL_BLEND_WEIGHT) * seasonal_v
                )

        if not allow_negative:
            pred = max(0.0, pred)

        if has_quantile_models:
            lower_raw = float(trained.gbrt_lower.predict(x_row_std)[0])
            upper_raw = float(trained.gbrt_upper.predict(x_row_std)[0])
            # A quantile GBRT has no ordering constraint between separately
            # fit lower/upper models -- clamp to a real non-negative band
            # around the point estimate rather than trust the raw pair.
            raw_half_widths.append(max(0.0, upper_raw - lower_raw) / 2.0)

        raw_preds.append(pred)
        buffer_times.append(ts)
        buffer_vals.append(pred)

    smoothed: list[float] = []
    prev = raw_preds[0] if raw_preds else 0.0
    for p, anchored in zip(raw_preds, seasonal_anchored, strict=True):
        # Skip damping entirely (alpha=1.0, trust the raw prediction as-
        # is) once a step is "seasonal_anchored" -- both its lag inputs
        # already come from TrainedModel.seasonal_lookup's pre-averaged
        # per-hour table, not noisy step-to-step momentum. Confirmed
        # live 2026-08-15, directly against real data: this household's
        # own P2P-sell-to-self-consume automation cuts battery discharge
        # from ~13kW to ~1-2kW in under a minute at exactly 00:00:00
        # every night (a real, deliberate, near-instant step, not
        # gradual decay) -- but DAMPING_ALPHA's exponential smoothing,
        # applied uniformly across the whole 96h sequence, was blurring
        # that genuine step into a fake ~45-minute ramp (00:00 -> 8.83,
        # 00:15 -> 3.28, 00:30 -> 2.11, 00:45 -> 1.70 instead of an
        # immediate drop). Damping still applies normally for the first
        # ~hour of any forecast (LAG_LONG_STEPS grid steps), where
        # consecutive raw predictions genuinely are closely correlated
        # (real recent momentum) and smoothing out step-to-step model
        # jitter is the whole point -- this is scoped narrowly to the
        # region where that reasoning no longer holds.
        alpha = 1.0 if anchored else DAMPING_ALPHA
        prev = alpha * p + (1 - alpha) * prev
        smoothed.append(prev)

    if not has_quantile_models:
        return PredictionResult(values=smoothed)

    if allow_negative:
        model_lower = [v - hw for v, hw in zip(smoothed, raw_half_widths, strict=True)]
    else:
        model_lower = [
            max(0.0, v - hw) for v, hw in zip(smoothed, raw_half_widths, strict=True)
        ]
    model_upper = [v + hw for v, hw in zip(smoothed, raw_half_widths, strict=True)]
    return PredictionResult(
        values=smoothed, model_lower=model_lower, model_upper=model_upper
    )
