"""Training and prediction for Nimbus's load model.

Deliberately plain and light: a RandomForestRegressor (scikit-learn), not a
heavier pipeline. Chosen because it's robust to noisy real-world sensor data
without careful hyperparameter tuning, and trains fast enough to redo daily
on modest hardware (a NUC, a Raspberry Pi running HAOS, etc.). If accuracy
ever needs a better model, swap it here -- the rest of the integration
(feature building, resampling, HA glue) doesn't need to change.

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
from sklearn.ensemble import RandomForestRegressor

from .features import build_features

_LOGGER = logging.getLogger(__name__)


@dataclass
class TrainedModel:
    """A fitted model plus the metadata worth exposing on the sensor."""

    model: RandomForestRegressor
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
    """Train a fresh model from real (local-time) history events.

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

    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=12,
        min_samples_leaf=5,
        n_jobs=-1,
        random_state=42,
    )
    model.fit(np.array(x_rows), np.array(y_vals))
    _LOGGER.info("Trained on %d points.", len(x_rows))

    return TrainedModel(model=model, trained_at=end, training_points=len(x_rows))


def predict(
    trained: TrainedModel,
    timestamps: list[datetime],
    temps: list[float],
) -> list[float]:
    """Predict load at each of `timestamps`, given a matching `temps` list."""
    x_rows = [build_features(ts, temp) for ts, temp in zip(timestamps, temps, strict=True)]
    preds = trained.model.predict(np.array(x_rows))
    return [max(0.0, float(p)) for p in preds]
