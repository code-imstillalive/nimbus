"""Regression tests for nimbus issue #360 finding 7 (Mark Purcell, codebase
review): "Nothing covers ... predict()'s allow_negative/seasonal_anchor/
damping-skip paths (CLAUDE.md bugs #1/#3/#4/#6/#7 have no regression
tests)." These three real, historically-documented bugs (see nimbus's own
CLAUDE.md, "Recursive-forecast bug chain") had never had a dedicated
automated test locking them in -- each was originally found and fixed by a
human reading a live chart, not by any test.

Same "ml/model.py has zero homeassistant.* imports, directly importable"
convention as this project's other ml test files.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
import numpy as np
from nimbus_load.ml import model as ml_model
from nimbus_load.ml.features import FEATURE_NAMES

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001
KNN_RAW_VALUE = 5.0


def _seasonal_signed_events(
    days: float, peak: float, trough: float
) -> list[tuple[datetime, float]]:
    """A clean, noise-free hourly signal oscillating between `trough`
    (real negative values, e.g. -30kW "charging") and `peak` (e.g. +10kW
    "discharging"), following a simple diurnal sine -- exactly the signed,
    seasonal shape a real Battery power-signal subentry has (see
    allow_negative's own docstring in model.py).
    """
    n_hours = int(days * 24)
    mid = (peak + trough) / 2.0
    amp = (peak - trough) / 2.0
    events = []
    for i in range(n_hours):
        t = START_OF_TIME + timedelta(hours=i)
        value = mid + amp * math.sin(2 * math.pi * (t.hour / 24.0))
        events.append((t, value))
    return events


class TestAllowNegative(unittest.TestCase):
    """Bug #1 (v0.13.0): every ML prediction was clamped to >= 0.0,
    correct for a load but wrong for a signed power-signal target."""

    def setUp(self):
        days = 20.0
        events = _seasonal_signed_events(days, peak=10.0, trough=-30.0)
        self.trained = ml_model.train_model(
            load_events=events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=START_OF_TIME,
            end=START_OF_TIME + timedelta(days=days),
            resample_minutes=60,
            min_training_points=50,
        )
        self.assertIsNotNone(self.trained)
        # Confirm the training data genuinely has real negative values --
        # otherwise a clamp-vs-no-clamp comparison would be meaningless.
        self.assertTrue(any(v < 0.0 for _, v in events))

        # Forecast starting right where training data ends, seeded with a
        # real recent negative reading (a real "currently charging" moment).
        forecast_start = START_OF_TIME + timedelta(days=days)
        self.timestamps = [forecast_start + timedelta(hours=i) for i in range(24)]
        self.recent_load_values = [
            (forecast_start - timedelta(hours=i), -20.0) for i in range(1, 6)
        ]

    def test_allow_negative_false_clamps_every_prediction_to_zero_or_above(self):
        result = ml_model.predict(
            trained=self.trained,
            timestamps=self.timestamps,
            temps=[20.0] * len(self.timestamps),
            humidities=[50.0] * len(self.timestamps),
            recent_load_values=self.recent_load_values,
            resample_minutes=60,
            allow_negative=False,
        )
        for v in result.values:
            self.assertGreaterEqual(v, 0.0)

    def test_allow_negative_true_lets_real_negative_predictions_through(self):
        result = ml_model.predict(
            trained=self.trained,
            timestamps=self.timestamps,
            temps=[20.0] * len(self.timestamps),
            humidities=[50.0] * len(self.timestamps),
            recent_load_values=self.recent_load_values,
            resample_minutes=60,
            allow_negative=True,
        )
        # The trained signal is genuinely negative for roughly half of
        # every 24h cycle -- a 24-point forecast starting at an arbitrary
        # hour should show at least some negative values once the clamp
        # is lifted.
        self.assertTrue(
            any(v < 0.0 for v in result.values),
            f"expected at least one negative prediction with allow_negative=True, got {result.values}",
        )


class TestSeasonalAnchorBlend(unittest.TestCase):
    """Bug #3 (v0.16.0): once a forecast step's own lag lookback runs past
    real recent history, its lag input used to come from the model's own
    prior (self-generated) prediction -- a chain that starts from an
    atypical/transitional "now" never reverts to the true seasonal
    pattern. seasonal_anchor=True fixes this by blending in the trained
    seasonal_lookup value once real lag data runs out.

    A genuine statistical black-box reproduction of this (train on noisy
    synthetic data, seed an atypical recent value, check late-horizon
    convergence) was tried first and abandoned: a well-fit model (GBRT
    won model selection on the synthetic data used) already tracks the
    real hour-of-day pattern closely enough on its own that the
    difference between seasonal_anchor=True/False washed out within a
    handful of steps -- consistent with nimbus's own CLAUDE.md note that
    even the ORIGINAL 2026-08-15 investigation needed real 45-day
    household data, not a clean synthetic signal, to get a reliable
    repro. This test instead pins the MECHANISM directly and
    deterministically: a hand-built model with a known, fixed raw output
    and a deliberately different, hand-set seasonal_lookup entry --
    unambiguous regardless of how well any particular model happens to
    fit any particular synthetic dataset.
    """

    def _build_trained_model(self, seasonal_value: float) -> ml_model.TrainedModel:
        # k-NN with a tightly-clustered y_train: its weighted average is
        # ~constant (close to KNN_RAW_VALUE) regardless of the query
        # features, giving a known, stable "raw model prediction" to
        # contrast against a deliberately different seasonal_lookup
        # value -- k-NN chosen specifically because (per this project's
        # own CLAUDE.md finding on GBRT/Grid) it's the one candidate
        # whose predictions are provably bounded within y_train's own
        # range, so a tightly-clustered y_train reliably yields a
        # tightly-clustered prediction.
        n_features = len(FEATURE_NAMES)
        rng = np.random.default_rng(7)
        n = 60
        x_train = rng.normal(size=(n, n_features))
        y_train = np.full(n, KNN_RAW_VALUE) + rng.normal(scale=0.01, size=n)

        anchor_ts = self.forecast_start + timedelta(hours=self.anchor_step)
        key = (anchor_ts.weekday(), anchor_ts.hour, anchor_ts.minute // 15)
        return ml_model.TrainedModel(
            model_type="knn",
            x_mean=np.zeros(n_features),
            x_std=np.ones(n_features),
            x_train=x_train,
            y_train=y_train,
            gbrt=None,
            trained_at=self.forecast_start,
            training_points=n,
            seasonal_lookup={key: seasonal_value},
        )

    def setUp(self):
        self.forecast_start = START_OF_TIME
        # LAG_LONG_STEPS=4, one real recent point one step before "now" --
        # step index 4 is the first step whose lag_long lookup itself
        # falls past that single real point, i.e. the first genuinely
        # seasonal-anchored step.
        self.anchor_step = 4
        self.seasonal_value = 500.0  # deliberately far from KNN_RAW_VALUE
        self.trained = self._build_trained_model(self.seasonal_value)
        self.timestamps = [
            self.forecast_start + timedelta(hours=i)
            for i in range(self.anchor_step + 1)
        ]
        self.recent_load_values = [
            (self.forecast_start - timedelta(hours=1), KNN_RAW_VALUE)
        ]

    def _predict_at_anchor_step(self, seasonal_anchor: bool) -> float:
        result = ml_model.predict(
            trained=self.trained,
            timestamps=self.timestamps,
            temps=[20.0] * len(self.timestamps),
            humidities=[50.0] * len(self.timestamps),
            recent_load_values=self.recent_load_values,
            resample_minutes=60,
            allow_negative=True,
            seasonal_anchor=seasonal_anchor,
        )
        return result.values[self.anchor_step]

    def test_seasonal_anchor_true_pulls_the_result_toward_the_seasonal_value(self):
        predicted = self._predict_at_anchor_step(seasonal_anchor=True)
        # 50/50 blend (SEASONAL_BLEND_WEIGHT) of a ~KNN_RAW_VALUE raw
        # prediction and the hand-set 500.0 seasonal value should land
        # roughly halfway -- nowhere near the raw model's own ~constant
        # output, and nowhere near the seasonal value alone either.
        self.assertGreater(predicted, KNN_RAW_VALUE + 50.0)
        self.assertLess(predicted, self.seasonal_value - 50.0)

    def test_seasonal_anchor_false_ignores_the_seasonal_value_entirely(self):
        predicted = self._predict_at_anchor_step(seasonal_anchor=False)
        # With the flag off, the seasonal_lookup entry must have zero
        # effect -- the result should stay close to the raw model's own
        # ~constant output, nowhere near the hand-set 500.0.
        self.assertLess(abs(predicted - KNN_RAW_VALUE), 20.0)
        self.assertLess(predicted, self.seasonal_value - 100.0)

    def test_flag_is_the_only_thing_that_changes_the_outcome(self):
        anchored = self._predict_at_anchor_step(seasonal_anchor=True)
        unanchored = self._predict_at_anchor_step(seasonal_anchor=False)
        self.assertGreater(
            anchored - unanchored,
            100.0,
            f"expected a large, unambiguous gap between anchored ({anchored}) "
            f"and unanchored ({unanchored}) given a 500.0-unit seasonal value "
            f"the raw model has no way of producing on its own",
        )


class TestDampingSkipForSeasonalAnchoredSteps(unittest.TestCase):
    """Bug #4 (v0.17.0): DAMPING_ALPHA's exponential smoothing used to
    apply uniformly across the whole forecast, blurring a genuine sharp
    level change (once seasonal-anchored) into a fake multi-step ramp.
    Once a step is seasonal-anchored, damping must be skipped entirely
    (alpha=1.0) so a real step transition lands within one grid step."""

    def test_a_sharp_seasonal_step_change_is_not_smeared_across_multiple_steps(self):
        # Two-state training data: a real seasonal LOW for hours 0-11,
        # a real seasonal HIGH for hours 12-23 -- a clean step function,
        # not a smooth sine, so any smearing across the boundary is
        # unambiguous and easy to detect.
        days = 20.0
        n_hours = int(days * 24)
        events = []
        for i in range(n_hours):
            t = START_OF_TIME + timedelta(hours=i)
            value = 2.0 if t.hour < 12 else 14.0
            events.append((t, value))
        trained = ml_model.train_model(
            load_events=events,
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=START_OF_TIME,
            end=START_OF_TIME + timedelta(days=days),
            resample_minutes=60,
            min_training_points=50,
        )
        self.assertIsNotNone(trained)

        # Forecast starts well before the boundary (hour 6), seeded with
        # real recent low-state data, so the model has already exhausted
        # its real lag data and gone fully seasonal-anchored several hours
        # before crossing the real 12:00 step boundary.
        forecast_start = START_OF_TIME + timedelta(days=days, hours=6)
        n_steps = 12
        timestamps = [forecast_start + timedelta(hours=i) for i in range(n_steps)]
        recent_load_values = [(forecast_start - timedelta(hours=1), 2.0)]

        result = ml_model.predict(
            trained=trained,
            timestamps=timestamps,
            temps=[20.0] * n_steps,
            humidities=[50.0] * n_steps,
            recent_load_values=recent_load_values,
            resample_minutes=60,
            seasonal_anchor=True,
        )

        # timestamps[6] is exactly hour 12 -- the real step boundary.
        boundary_index = 6
        self.assertEqual(timestamps[boundary_index].hour, 12)
        at_boundary = result.values[boundary_index]
        # A smeared (damped) transition would still be well below the
        # real high-state value (14.0) right at the boundary step; an
        # undamped, seasonal-anchored transition should land close to it
        # within that same single step.
        self.assertGreater(
            at_boundary,
            10.0,
            f"expected the step exactly at the real boundary to already be close "
            f"to the new seasonal value (14.0), got {at_boundary} -- looks smeared",
        )


if __name__ == "__main__":
    unittest.main()
