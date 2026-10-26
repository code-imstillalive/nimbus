"""The Forecaster's points sit on the clock (#1837)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.coordinator import (
    _forecast_grid_utc,
    _predict_now_then_clock,
)
from custom_components.nimbus_load.ml.model import PredictionResult

NOW = datetime(2026, 10, 13, 4, 28, 30, 287956, tzinfo=UTC)  # 14:28:30 AEST


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
