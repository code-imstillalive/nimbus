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

Every function in this module is blocking / CPU-bound and must always be
called from an executor thread (`hass.async_add_executor_job`), never
directly on Home Assistant's event loop -- see coordinator.py.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

import numpy as np

from .features import build_features
from .gbrt import GBRT
from ..const import LAG_LONG_STEPS, LAG_SHORT_STEPS, VALIDATION_HOLDOUT_FRACTION

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

# GBRT hyperparameters. Deliberately shallow/few trees -- this trains once
# a day inside a HA executor thread on a few thousand rows, not a
# standalone training job; kept small enough that it stays comfortably
# fast at that scale rather than tuned for maximum accuracy regardless of
# runtime.
GBRT_N_ESTIMATORS = 60
GBRT_MAX_DEPTH = 3
GBRT_LEARNING_RATE = 0.1
GBRT_MIN_SAMPLES_LEAF = 5


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


def _build_grid(start: datetime, end: datetime, resample_minutes: int) -> list[datetime]:
    step = timedelta(minutes=resample_minutes)
    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += step
    return grid


def _knn_predict_batch(
    x_mean: np.ndarray, x_std: np.ndarray, x_train: np.ndarray, y_train: np.ndarray,
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
    start: datetime,
    end: datetime,
    resample_minutes: int,
    min_training_points: int,
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
    temp_vals = resample_last_value(temp_events, grid) if temp_events else [None] * len(grid)
    humidity_vals = (
        resample_last_value(humidity_events, grid) if humidity_events else [None] * len(grid)
    )

    x_rows: list[list[float]] = []
    y_vals: list[float] = []
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
        x_rows.append(build_features(grid[i], tv, hv, lag_short_v, lag_long_v))
        y_vals.append(lv)

    if len(x_rows) < min_training_points:
        _LOGGER.warning(
            "Only %d usable training points (need >= %d) -- skipping this cycle.",
            len(x_rows), min_training_points,
        )
        return None

    x_all = np.array(x_rows, dtype=np.float64)
    y_all = np.array(y_vals, dtype=np.float64)

    # Chronological split -- x_rows/y_vals are already in time order since
    # the grid itself is, so a plain index cut is a real chronological
    # split, not a random one.
    split = int(len(x_all) * (1 - VALIDATION_HOLDOUT_FRACTION))
    split = max(split, min_training_points // 2)  # keep a real training set even on a small window
    x_tr, y_tr = x_all[:split], y_all[:split]
    x_val, y_val = x_all[split:], y_all[split:]

    x_mean = x_tr.mean(axis=0)
    x_std = x_tr.std(axis=0)
    x_std[x_std < 1e-9] = 1.0  # avoid divide-by-zero on a constant feature column
    x_tr_std = (x_tr - x_mean) / x_std

    validation_mae: dict[str, float] = {}
    model_type = "knn"  # safe default if validation set is too small to compare meaningfully

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
            n_estimators=GBRT_N_ESTIMATORS, max_depth=GBRT_MAX_DEPTH,
            learning_rate=GBRT_LEARNING_RATE, min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
        )
        gbrt_val.fit(x_tr_std, y_tr)
        gbrt_val_pred = gbrt_val.predict(x_val_std)
        validation_mae["gbrt"] = _mae(y_val, gbrt_val_pred)

        model_type = "gbrt" if validation_mae["gbrt"] < validation_mae["knn"] else "knn"
        _LOGGER.info(
            "Model validation: knn_mae=%.4f gbrt_mae=%.4f -> using %s",
            validation_mae["knn"], validation_mae["gbrt"], model_type,
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
            n_estimators=GBRT_N_ESTIMATORS, max_depth=GBRT_MAX_DEPTH,
            learning_rate=GBRT_LEARNING_RATE, min_samples_leaf=GBRT_MIN_SAMPLES_LEAF,
        )
        gbrt_final.fit(x_all_std, y_all)

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
    )


def predict(
    trained: TrainedModel,
    timestamps: list[datetime],
    temps: list[float],
    humidities: list[float],
    recent_load_values: list[tuple[datetime, float]],
    resample_minutes: int,
) -> list[float]:
    """Predict load at each of `timestamps` (must be ascending, evenly
    spaced by `resample_minutes`), given matching `temps`/`humidities`.

    Recursive multi-step forecast: the first few steps can use REAL recent
    history (`recent_load_values`) for their lag features; once the
    horizon extends past that real history, each step's own just-made
    prediction becomes the lag input for later steps -- standard practice
    for lag-feature time-series forecasting, and the only option, since no
    real future data exists yet to use instead. This is also why this
    function predicts one step at a time rather than a single vectorized
    batch, unlike the old lag-free k-NN predict().

    A light exponential-smoothing pass (DAMPING_ALPHA) is applied to the
    raw output sequence before returning, so consecutive 15-minute steps
    don't jump unrealistically -- pure post-processing, doesn't feed back
    into the model itself.
    """
    step = timedelta(minutes=resample_minutes)
    # Rolling buffer of (timestamp, value), seeded from real history, that
    # we append our own predictions onto as we go -- this IS the lag
    # source for every step.
    buffer = sorted(recent_load_values, key=lambda p: p[0])
    buffer_times = [p[0] for p in buffer]
    buffer_vals = [p[1] for p in buffer]
    default_lag = float(np.mean(trained.y_train)) if len(trained.y_train) else 0.0

    def lag_at(target: datetime) -> float:
        idx = bisect_right(buffer_times, target) - 1
        return buffer_vals[idx] if idx >= 0 else default_lag

    raw_preds: list[float] = []
    for ts, temp, humidity in zip(timestamps, temps, humidities, strict=True):
        lag_short_t = ts - LAG_SHORT_STEPS * step
        lag_long_t = ts - LAG_LONG_STEPS * step
        lag_short_v = lag_at(lag_short_t)
        lag_long_v = lag_at(lag_long_t)

        x_row = np.array(
            [build_features(ts, temp, humidity, lag_short_v, lag_long_v)], dtype=np.float64
        )
        x_row_std = (x_row - trained.x_mean) / trained.x_std

        if trained.model_type == "gbrt" and trained.gbrt is not None:
            pred = float(trained.gbrt.predict(x_row_std)[0])
        else:
            pred = float(_knn_predict_batch(
                trained.x_mean, trained.x_std, trained.x_train, trained.y_train, x_row
            )[0])
        pred = max(0.0, pred)

        raw_preds.append(pred)
        buffer_times.append(ts)
        buffer_vals.append(pred)

    smoothed: list[float] = []
    prev = raw_preds[0] if raw_preds else 0.0
    for p in raw_preds:
        prev = DAMPING_ALPHA * p + (1 - DAMPING_ALPHA) * prev
        smoothed.append(prev)
    return smoothed
