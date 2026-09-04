"""Regression test for nimbus issue #366 finding 2 (per-cycle loop work):
ml/model.py's predict() used to call trained.gbrt_lower.predict()/
trained.gbrt_upper.predict() once PER HORIZON STEP (~385 times/tick in
production) even though neither quantile model feeds the recursive lag
chain -- only the main model's own `pred` does. Fixed by collecting every
step's standardized feature row and calling each quantile model's
predict() exactly once, in a single batch, after the main loop.

This is verified two ways:
1. A call-count assertion (wrapping the real GBRT.predict so it still
   executes) proving the batching genuinely happened, not just a cosmetic
   refactor that still loops internally.
2. A numeric check that model_lower/model_upper, derived from the single
   batched call, exactly match what calling gbrt_lower/gbrt_upper.predict()
   one row at a time (the OLD calling convention) on the same captured
   rows would have produced -- relying on GBRT.predict() being genuinely
   row-independent (see tests/test_gbrt_split_vectorization_equivalence.py
   for _build_tree()'s own equivalence guarantee; this test instead pins
   predict()'s batch-vs-looped invariant, the assumption this specific
   model.py refactor depends on).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _ml_path  # noqa: F401
import numpy as np
from nimbus_load.ml import gbrt as gbrt_module
from nimbus_load.ml import model as ml_model
from nimbus_load.ml.features import FEATURE_NAMES

START = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _build_trained_model_with_quantiles() -> ml_model.TrainedModel:
    n_features = len(FEATURE_NAMES)
    rng = np.random.default_rng(123)
    x_train = rng.normal(size=(200, n_features))
    y_train = rng.normal(size=200) * 3.0 + 5.0

    gbrt_mean = gbrt_module.GBRT(n_estimators=10, max_depth=3, min_samples_leaf=5)
    gbrt_mean.fit(x_train, y_train)

    gbrt_lower = gbrt_module.GBRT(
        n_estimators=10, max_depth=3, min_samples_leaf=5, quantile=0.1
    )
    gbrt_lower.fit(x_train, y_train)

    gbrt_upper = gbrt_module.GBRT(
        n_estimators=10, max_depth=3, min_samples_leaf=5, quantile=0.9
    )
    gbrt_upper.fit(x_train, y_train)

    return ml_model.TrainedModel(
        model_type="gbrt",
        x_mean=np.zeros(n_features),
        x_std=np.ones(n_features),
        x_train=x_train,
        y_train=y_train,
        gbrt=gbrt_mean,
        trained_at=START,
        training_points=200,
        gbrt_lower=gbrt_lower,
        gbrt_upper=gbrt_upper,
    )


class TestPredictBatchesQuantileInference(unittest.TestCase):
    def setUp(self):
        self.trained = _build_trained_model_with_quantiles()
        self.timestamps = [START + timedelta(hours=i) for i in range(50)]
        self.recent_load_values = [
            (START - timedelta(minutes=15 * i), 5.0) for i in range(1, 20)
        ]

    def test_quantile_models_each_predict_exactly_once_per_cycle(self):
        original_predict = gbrt_module.GBRT.predict
        call_shapes: dict[int, list[tuple[int, ...]]] = {}

        def counting_predict(self, x):
            call_shapes.setdefault(id(self), []).append(np.asarray(x).shape)
            return original_predict(self, x)

        with patch.object(gbrt_module.GBRT, "predict", counting_predict):
            result = ml_model.predict(
                trained=self.trained,
                timestamps=self.timestamps,
                temps=[15.0] * len(self.timestamps),
                humidities=[50.0] * len(self.timestamps),
                recent_load_values=self.recent_load_values,
                resample_minutes=60,
            )

        # One call each for gbrt_lower and gbrt_upper -- NOT one per
        # timestamp. The mean model (trained.gbrt) is intentionally
        # excluded: its per-step calls are the real recursive lag chain
        # and must stay one-per-step.
        self.assertEqual(len(call_shapes[id(self.trained.gbrt_lower)]), 1)
        self.assertEqual(len(call_shapes[id(self.trained.gbrt_upper)]), 1)
        (lower_call_shape,) = call_shapes[id(self.trained.gbrt_lower)]
        (upper_call_shape,) = call_shapes[id(self.trained.gbrt_upper)]
        self.assertEqual(lower_call_shape, (len(self.timestamps), len(FEATURE_NAMES)))
        self.assertEqual(upper_call_shape, (len(self.timestamps), len(FEATURE_NAMES)))

        self.assertIsNotNone(result.model_lower)
        self.assertIsNotNone(result.model_upper)
        self.assertEqual(len(result.model_lower), len(self.timestamps))
        self.assertEqual(len(result.model_upper), len(self.timestamps))

    def test_batched_half_widths_match_row_by_row_predict(self):
        captured_rows: dict[int, np.ndarray] = {}
        original_predict = gbrt_module.GBRT.predict

        def capturing_predict(self, x):
            captured_rows[id(self)] = np.asarray(x)
            return original_predict(self, x)

        with patch.object(gbrt_module.GBRT, "predict", capturing_predict):
            result = ml_model.predict(
                trained=self.trained,
                timestamps=self.timestamps,
                temps=[15.0] * len(self.timestamps),
                humidities=[50.0] * len(self.timestamps),
                recent_load_values=self.recent_load_values,
                resample_minutes=60,
            )

        x_all = captured_rows[id(self.trained.gbrt_lower)]
        # Ground truth: the OLD one-row-at-a-time calling convention,
        # applied to the exact rows the real (batched) code path used.
        expected_lower = np.array(
            [self.trained.gbrt_lower.predict(row.reshape(1, -1))[0] for row in x_all]
        )
        expected_upper = np.array(
            [self.trained.gbrt_upper.predict(row.reshape(1, -1))[0] for row in x_all]
        )
        expected_half_widths = np.maximum(0.0, expected_upper - expected_lower) / 2.0

        actual_half_widths = np.array(
            [u - v for u, v in zip(result.model_upper, result.values, strict=True)]
        )
        np.testing.assert_allclose(actual_half_widths, expected_half_widths, atol=1e-9)
