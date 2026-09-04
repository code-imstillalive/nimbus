"""Regression test for nimbus issue #353 (Mark Purcell, codebase review),
Defect 1: a single "nan" event fed into train_model() (float("nan") does
not raise, so it can slip past the existing try/except (TypeError,
ValueError) parsing at the coordinator.py fetch layer for a template/
REST/Modbus sensor without a numeric device_class/state_class) used to
poison the whole retrain -- one NaN training row makes x_mean/x_std NaN,
every candidate's validation_mae comes out NaN, min() over a NaN dict
still "picks" a model, and predict() then silently returns 0.0 for every
step (max(0.0, nan) == 0.0), with no error at all.

This test exercises the REAL train_model() directly with a synthetic
event list containing one NaN reading -- deliberately NOT going through
coordinator.py's own fetch-layer guard (also fixed, see
tests/test_coordinator_lts_hybrid_training.py and
tests/test_coordinator_helpers.py for those), to prove ml/model.py's own
defensive isfinite check holds even for a caller that bypasses the
coordinator entirely (e.g. a future different data source, or a test).
Same "ml/model.py has zero homeassistant.* imports, directly importable"
convention as this project's other ml test files.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _hourly_load_events_with_one_nan(days: float) -> list[tuple[datetime, float]]:
    n_hours = int(days * 24)
    events = []
    for i in range(n_hours):
        t = START_OF_TIME + timedelta(hours=i)
        value = 3.0 + 2.0 * math.sin(2 * math.pi * (t.hour / 24.0)) + i * 0.01
        events.append((t, value))
    # Poison exactly one real event, well inside the window, with NaN.
    mid = n_hours // 2
    t_mid, _ = events[mid]
    events[mid] = (t_mid, float("nan"))
    return events


class TestTrainModelNanPoisoning(unittest.TestCase):
    def test_a_single_nan_event_does_not_poison_validation_mae(self):
        days = 15.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events_with_one_nan(days)

        trained = ml_model.train_model(
            load_events=load_events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=60,
            min_training_points=50,
        )

        self.assertIsNotNone(trained)
        for name, mae in trained.validation_mae.items():
            self.assertTrue(
                math.isfinite(mae), f"validation_mae[{name!r}] is not finite: {mae}"
            )

    def test_a_single_nan_event_does_not_poison_the_seasonal_lookup(self):
        days = 15.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events_with_one_nan(days)

        trained = ml_model.train_model(
            load_events=load_events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=60,
            min_training_points=50,
        )

        self.assertIsNotNone(trained)
        self.assertGreater(len(trained.seasonal_lookup), 0)
        for bucket, value in trained.seasonal_lookup.items():
            self.assertTrue(
                math.isfinite(value), f"seasonal_lookup[{bucket!r}] is not finite"
            )

    def test_a_single_nan_event_does_not_break_a_real_predict_call(self):
        days = 15.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events_with_one_nan(days)

        trained = ml_model.train_model(
            load_events=load_events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=start,
            end=end,
            resample_minutes=60,
            min_training_points=50,
        )
        self.assertIsNotNone(trained)

        timestamps = [end + timedelta(hours=h) for h in range(1, 7)]
        result = ml_model.predict(
            trained=trained,
            timestamps=timestamps,
            temps=[22.0] * len(timestamps),
            humidities=[50.0] * len(timestamps),
            recent_load_values=load_events[-8:],
            resample_minutes=60,
        )
        for v in result.values:
            self.assertTrue(math.isfinite(v), f"predicted value is not finite: {v}")
            # The real regression this issue describes: a poisoned model
            # silently returns exactly 0.0 for every step. A healthy model
            # trained on this sine-wave-plus-drift series should not.
        self.assertFalse(
            all(v == 0.0 for v in result.values),
            "every predicted value is exactly 0.0 -- looks like the "
            "poisoned-model regression this issue describes",
        )


if __name__ == "__main__":
    unittest.main()
