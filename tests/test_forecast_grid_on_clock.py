"""Forecaster points sit on the clock (household, 13 Oct 2026).

Every load and signal forecast stepped from the moment its sensor last
refreshed, so its points landed at 14:28:30, 14:43:30, ... on one circuit and
14:29:53, 14:44:53, ... on the next, while prices and the plan step on
:00/:15/:30/:45. The grid is now "now", then each quarter-hour of the clock.
`predict()` needs evenly spaced timestamps, so the on-clock run is predicted as
one sequence and "now" on its own.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import (
    _forecast_grid_utc,
    _predict_now_then_clock,
)
from custom_components.nimbus_load.ml import model as ml_model
from custom_components.nimbus_load.ml.model import PredictionResult

NOW = datetime(2026, 10, 13, 4, 28, 30, 287956, tzinfo=UTC)  # 14:28:30 AEST


def test_first_point_is_now_and_the_rest_are_on_the_clock():
    grid = _forecast_grid_utc(NOW, NOW + timedelta(hours=2))
    assert grid[0] == NOW
    assert grid[1] == datetime(2026, 10, 13, 4, 30, tzinfo=UTC)
    assert all(
        t.minute % 15 == 0 and t.second == 0 and t.microsecond == 0 for t in grid[1:]
    )
    assert all(b - a == timedelta(minutes=15) for a, b in pairwise(grid[1:]))
    assert grid[-1] == datetime(2026, 10, 13, 6, 15, tzinfo=UTC)


def test_now_already_on_the_clock_is_not_repeated():
    on = datetime(2026, 10, 13, 4, 30, tzinfo=UTC)
    grid = _forecast_grid_utc(on, on + timedelta(minutes=45))
    assert grid == [on + timedelta(minutes=15 * i) for i in range(4)]


def test_the_end_is_included_when_it_falls_on_the_clock():
    grid = _forecast_grid_utc(NOW, datetime(2026, 10, 13, 5, 0, tzinfo=UTC))
    assert grid[-1] == datetime(2026, 10, 13, 5, 0, tzinfo=UTC)


def test_on_the_local_clock_too():
    # Brisbane is UTC+10, so a UTC quarter-hour is a local one.
    from zoneinfo import ZoneInfo

    local = [
        t.astimezone(ZoneInfo("Australia/Brisbane"))
        for t in _forecast_grid_utc(NOW, NOW + timedelta(hours=1))
    ]
    assert [t.strftime("%H:%M") for t in local[1:]] == [
        "14:30",
        "14:45",
        "15:00",
        "15:15",
    ]


def test_now_and_the_clock_run_are_predicted_separately_and_joined():
    calls = []

    def fake(lo, hi):
        calls.append((lo, hi))
        n = 1 if hi == 1 else 4
        return PredictionResult(
            values=[float(lo)] * n, model_lower=[0.0] * n, model_upper=[9.0] * n
        )

    out = _predict_now_then_clock(fake, 5)
    assert calls == [(0, 1), (1, None)]
    assert out.values == [0.0, 1.0, 1.0, 1.0, 1.0]
    assert out.model_lower == [0.0] * 5 and out.model_upper == [9.0] * 5


def test_bounds_are_dropped_unless_both_parts_have_them():
    def fake(lo, hi):
        if hi == 1:
            return PredictionResult(values=[1.0])
        return PredictionResult(
            values=[2.0, 3.0], model_lower=[0.0, 0.0], model_upper=[5.0, 5.0]
        )

    out = _predict_now_then_clock(fake, 3)
    assert out.values == [1.0, 2.0, 3.0]
    assert out.model_lower is None and out.model_upper is None


def test_a_single_point_grid_is_one_call():
    calls = []

    def fake(lo, hi):
        calls.append((lo, hi))
        return PredictionResult(values=[7.0])

    assert _predict_now_then_clock(fake, 1).values == [7.0]
    assert calls == [(0, None)]


def test_a_real_model_forecasts_its_learned_run_on_the_clock():
    # A load that draws 2 kW from 05:00 to 06:00 UTC every day, 0.1 kW
    # otherwise, trained on three weeks. Its forecast from 04:28:30 should
    # show the run in the 05:00-05:45 points, which only exist on the clock.
    start = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    events = []
    t = start
    while t < NOW:
        events.append((t, 2.0 if t.hour == 5 else 0.1))
        t += timedelta(minutes=15)
    trained = ml_model.train_model(
        load_events=events,
        temp_events=[],
        humidity_events=[],
        curtailment_events=[],
        start=start,
        end=NOW,
        resample_minutes=15,
        min_training_points=50,
    )
    assert trained is not None
    grid = _forecast_grid_utc(NOW, NOW + timedelta(hours=3))
    recent = [(NOW - timedelta(minutes=15 * i), 0.1) for i in range(8)]

    def run(lo, hi):
        ts = grid[lo:hi]
        return ml_model.predict(
            trained=trained,
            timestamps=ts,
            temps=[20.0] * len(ts),
            humidities=[50.0] * len(ts),
            recent_load_values=recent,
            resample_minutes=15,
        )

    out = _predict_now_then_clock(run, len(grid))
    assert len(out.values) == len(grid)
    by_time = dict(zip(grid, out.values, strict=True))
    run_points = [
        by_time[datetime(2026, 10, 13, 5, m, tzinfo=UTC)] for m in (0, 15, 30, 45)
    ]
    idle_points = [by_time[datetime(2026, 10, 13, 7, m, tzinfo=UTC)] for m in (0, 15)]
    assert min(run_points) > 1.0
    assert max(idle_points) < 1.0
