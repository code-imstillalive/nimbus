"""Tests for the training-diagnostic fields added to TrainedModel (2026-08-26,
nimbus issue #113, Mark Purcell): "if MASE is meant to be computed, it isn't"
and "the dump should say what resolution it trained at and what window it
actually got, because '30 days' is currently aspirational."

Real, from-scratch synthetic data through the actual train_model() -- not a
reimplementation -- same convention as this project's other pure-python
ml/solver test files (see CLAUDE.md's own "Testing" section: ml/model.py has
zero homeassistant.* imports, so it's directly importable here).
"""

import math
import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _hourly_load_events(days: float, start: datetime = START_OF_TIME):
    """A real, smoothly-varying synthetic load series -- a daily sine wave
    plus a slow linear drift, at 1-reading-per-hour cadence. Deterministic
    (no random seed juggling needed) so these tests never flake.

    The drift matters: a purely 24h-periodic signal repeats EXACTLY every
    168 hours (a week), making every week-over-week diff MASE's own scale
    needs come out as exactly 0.0 -- a real footgun hit writing this test,
    not a hypothetical one. The drift term breaks that periodicity so
    week-over-week differences are genuinely nonzero, like a real load.
    """
    n_hours = int(days * 24)
    events = []
    for i in range(n_hours):
        t = start + timedelta(hours=i)
        value = 3.0 + 2.0 * math.sin(2 * math.pi * (t.hour / 24.0)) + i * 0.01
        events.append((t, value))
    return events


class TestTrainingSpanAndResolution(unittest.TestCase):
    def test_training_span_and_resample_minutes_reported(self):
        days = 15.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events(days)

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
        self.assertEqual(trained.resample_minutes, 60)
        # The real elapsed span of the rows that actually survived lag
        # filtering -- close to the full 15-day window (LAG_LONG_STEPS
        # hours are trimmed off the front), not the configured train_days
        # in the abstract and not just a row count a reader has to
        # convert by hand.
        self.assertGreater(trained.training_span_days, 14.0)
        self.assertLessEqual(trained.training_span_days, 15.0)


class TestMaseScalePointsVisibility(unittest.TestCase):
    def test_enough_history_populates_mase_and_reports_a_real_point_count(self):
        # 15 days comfortably clears the 7-day week-ago lookback PLUS
        # MIN_MASE_SCALE_POINTS(20) worth of training rows beyond that.
        days = 15.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events(days)

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
        self.assertGreaterEqual(trained.mase_scale_points, 20)
        self.assertIn("knn", trained.validation_mase)
        self.assertIn("gbrt", trained.validation_mase)

    def test_insufficient_history_reports_zero_points_not_a_silent_empty_dict(self):
        # Only 5 real days -- genuinely less than the 7-day week-ago
        # lookback MASE's own scale needs, so NO row can have a valid
        # week-ago value. Nimbus issue #113: this must be visible as an
        # honest "0 points found", not just an unexplained {}.
        days = 5.0
        start = START_OF_TIME
        end = start + timedelta(days=days)
        load_events = _hourly_load_events(days)

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
        self.assertEqual(trained.mase_scale_points, 0)
        self.assertEqual(trained.validation_mase, {})
        # The raw MAE comparison itself is NOT gated on a week of history
        # (only MASE's own scale is) -- still populated, proving the two
        # are genuinely independent gates, not the same one reported twice.
        self.assertIn("knn", trained.validation_mae)
