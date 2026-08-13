"""Training and prediction for Nimbus's load model.

Deliberately plain and light: a pure-numpy weighted k-nearest-neighbors
regressor, not a heavier ML library. Originally built on scikit-learn's
RandomForestRegressor, but confirmed live (2026-08-14) that scikit-learn has
no pre-built wheel for Python 3.14 yet and fails to build from source inside
Home Assistant's own container -- there's no C compiler present at all
(`cc`/`gcc`/`clang` all missing). numpy itself already installs cleanly on
this same Python version (same package HAEO's own manifest.json already
depends on), so a numpy-only model sidesteps the problem entirely rather
than waiting on it -- and is arguably a better fit for "runs anywhere HA
runs" than a package with a fragile build story on a brand-new Python
release.

k-NN suits this feature set naturally: the cyclic time-of-day/day-of-week/
month features plus temperature define a real, meaningful distance --
"find past moments that looked like this one, average what the load was
then." There's no real training step beyond standardizing and storing the
data (a "lazy learner") -- all the actual work happens at prediction time.

Every function in this module is blocking / CPU-bound and must always be
called from an executor thread (`hass.async_add_executor_job`), never
directly on Home Assistant's event loop -- see coordinator.py.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

import numpy as np

from .features import build_features

_LOGGER = logging.getLogger(__name__)

# Number of nearest neighbors averaged per prediction. Small enough to stay
# local (real seasonal/time-of-day structure), large enough to smooth out
# noisy individual readings.
K_NEIGHBORS = 15
# Added to distances before inverting to weights, so an exact (or
# near-exact) match doesn't produce a division by ~zero.
DISTANCE_EPSILON = 1e-6


@dataclass
class TrainedModel:
    """Training data plus normalization stats -- a k-NN model IS its
    (standardized) training set; there's no separate fitted estimator to
    store, unlike a tree/forest-based model.
    """

    x_mean: np.ndarray
    x_std: np.ndarray
    x_train: np.ndarray  # standardized feature rows, shape (n, n_features)
    y_train: np.ndarray  # matching load values, shape (n,)
    trained_at: datetime
    training_points: int


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


def train_model(
    *,
    load_events: list[tuple[datetime, float]],
    temp_events: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    resample_minutes: int,
    min_training_points: int,
) -> TrainedModel | None:
    """Build a fresh model from real (local-time) history events.

    Returns None (logging why) rather than raising, so a bad training cycle
    never takes the integration down -- the coordinator just keeps using
    whatever model it already has (or none yet) and tries again next cycle.
    """
    if not load_events:
        _LOGGER.warning("No load history available -- skipping this training cycle.")
        return None

    grid = _build_grid(start, end, resample_minutes)
    load_vals = resample_last_value(load_events, grid)
    temp_vals = resample_last_value(temp_events, grid) if temp_events else [None] * len(grid)

    x_rows: list[list[float]] = []
    y_vals: list[float] = []
    for i, g in enumerate(grid):
        lv = load_vals[i]
        if lv is None:
            continue
        tv = temp_vals[i] if temp_vals[i] is not None else 22.0
        x_rows.append(build_features(g, tv))
        y_vals.append(lv)

    if len(x_rows) < min_training_points:
        _LOGGER.warning(
            "Only %d usable training points (need >= %d) -- skipping this cycle.",
            len(x_rows), min_training_points,
        )
        return None

    x_train = np.array(x_rows, dtype=np.float64)
    y_train = np.array(y_vals, dtype=np.float64)

    x_mean = x_train.mean(axis=0)
    x_std = x_train.std(axis=0)
    x_std[x_std < 1e-9] = 1.0  # avoid divide-by-zero on a constant feature column
    x_train_std = (x_train - x_mean) / x_std

    _LOGGER.info("Trained on %d points.", len(x_rows))
    return TrainedModel(
        x_mean=x_mean,
        x_std=x_std,
        x_train=x_train_std,
        y_train=y_train,
        trained_at=end,
        training_points=len(x_rows),
    )


def predict(
    trained: TrainedModel,
    timestamps: list[datetime],
    temps: list[float],
) -> list[float]:
    """Predict load at each of `timestamps`, given a matching `temps` list.

    Weighted k-nearest-neighbors: for each query point, standardize using
    the training set's own mean/std, find the K closest training points by
    Euclidean distance, and average their loads weighted by inverse
    distance (closer matches count more).
    """
    x_query = np.array(
        [build_features(ts, temp) for ts, temp in zip(timestamps, temps, strict=True)],
        dtype=np.float64,
    )
    x_query_std = (x_query - trained.x_mean) / trained.x_std

    k = min(K_NEIGHBORS, len(trained.y_train))
    preds: list[float] = []
    for row in x_query_std:
        dists = np.sqrt(np.sum((trained.x_train - row) ** 2, axis=1))
        nearest_idx = np.argpartition(dists, k - 1)[:k]
        nearest_dists = dists[nearest_idx]
        weights = 1.0 / (nearest_dists + DISTANCE_EPSILON)
        pred = float(np.sum(weights * trained.y_train[nearest_idx]) / np.sum(weights))
        preds.append(max(0.0, pred))
    return preds
