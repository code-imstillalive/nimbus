"""The Forecaster's points sit on the clock (#1837)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.coordinator import (
    _predict_now_then_clock,
)
from custom_components.nimbus_load.ml.model import PredictionResult

NOW = datetime(2026, 10, 13, 4, 28, 30, 287956, tzinfo=UTC)  # 14:28:30 AEST


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


def test_a_single_point_grid_is_one_call():
    calls = []

    def fake(lo, hi):
        calls.append((lo, hi))
        return PredictionResult(values=[7.0])

    assert _predict_now_then_clock(fake, 1).values == [7.0]
    assert calls == [(0, None)]
