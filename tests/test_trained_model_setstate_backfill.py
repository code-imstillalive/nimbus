"""Regression test for nimbus issue #366 finding 3 (Mark Purcell, codebase
review): TrainedModel had no schema versioning and several fields
(gbrt_lower, gbrt_upper, model_type, validation_mae/validation_mase) were
accessed directly rather than via the defensive getattr(..., default)
pattern CLAUDE.md already established for seasonal_lookup -- a plain
@dataclass's default pickling restores an old persisted object's __dict__
verbatim on unpickle, skipping __init__ and every field default entirely,
so any field added after some already-deployed .pkl was written is
genuinely MISSING (not merely defaulted) from that object once unpickled.

Fixed with TrainedModel.__setstate__, which seeds every current field's
default before overlaying the pickle's own real state -- backfilling any
field an old pickle lacks, so direct attribute access is safe everywhere
without a defensive getattr() at every call site.

Uses the exact object.__new__() + manual __dict__ technique CLAUDE.md's
own prior seasonal_lookup fix used, round-tripped through the REAL pickle
protocol (not calling __setstate__ directly) so this exercises the genuine
on-disk failure mode, not a reimplementation of it.
"""

from __future__ import annotations

import pickle
import unittest
from datetime import datetime

import _ml_path  # noqa: F401
import numpy as np
from nimbus_load.ml import model as ml_model

START = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001


def _minimal_old_pickle_dict(n_features: int = 3) -> dict:
    """The full set of fields TrainedModel had on the very first day it
    existed -- deliberately excludes every field added later (schema_
    version, gbrt_lower/gbrt_upper, seasonal_lookup, mase_scale_points,
    resample_minutes, training_span_days, validation_recursive_mae).
    """
    return {
        "model_type": "knn",
        "x_mean": np.zeros(n_features),
        "x_std": np.ones(n_features),
        "x_train": np.zeros((5, n_features)),
        "y_train": np.zeros(5),
        "gbrt": None,
        "trained_at": START,
        "training_points": 5,
    }


class TestTrainedModelSetstateBackfillsOldPickles(unittest.TestCase):
    def test_a_pickle_from_before_any_of_these_fields_existed_backfills_all(self):
        old = object.__new__(ml_model.TrainedModel)
        old.__dict__ = _minimal_old_pickle_dict()

        restored = pickle.loads(pickle.dumps(old))

        self.assertEqual(restored.model_type, "knn")
        self.assertEqual(restored.training_points, 5)
        self.assertIsNone(restored.gbrt_lower)
        self.assertIsNone(restored.gbrt_upper)
        self.assertEqual(restored.validation_mae, {})
        self.assertEqual(restored.validation_mase, {})
        self.assertEqual(restored.validation_recursive_mae, {})
        self.assertEqual(restored.mase_scale_points, 0)
        self.assertEqual(restored.resample_minutes, 0)
        self.assertEqual(restored.training_span_days, 0.0)
        self.assertEqual(restored.seasonal_lookup, {})
        self.assertEqual(restored.schema_version, 0)

    def test_direct_attribute_access_does_not_raise_on_a_backfilled_field(self):
        # The real regression this fix guards against: before __setstate__
        # existed, `restored.gbrt_lower` on an old pickle raised a bare
        # AttributeError (this field is accessed directly, not via
        # getattr, at several call sites).
        old = object.__new__(ml_model.TrainedModel)
        old.__dict__ = _minimal_old_pickle_dict()
        restored = pickle.loads(pickle.dumps(old))

        try:
            _ = restored.gbrt_lower
            _ = restored.gbrt_upper
            _ = restored.validation_mae
            _ = restored.validation_mase
            _ = restored.schema_version
        except AttributeError as exc:
            self.fail(f"direct attribute access raised on a backfilled field: {exc}")

    def test_a_current_pickle_round_trips_its_real_state_unchanged(self):
        # The overlay must not clobber real, present values with defaults
        # -- only fields genuinely ABSENT from the pickle's own state
        # should fall back to the seeded default.
        fresh = ml_model.TrainedModel(
            model_type="gbrt",
            x_mean=np.zeros(3),
            x_std=np.ones(3),
            x_train=np.zeros((5, 3)),
            y_train=np.zeros(5),
            gbrt=None,
            trained_at=START,
            training_points=5,
            validation_mae={"gbrt": 1.23},
            mase_scale_points=42,
            resample_minutes=15,
            training_span_days=30.0,
        )
        self.assertEqual(fresh.schema_version, ml_model.TRAINED_MODEL_SCHEMA_VERSION)

        restored = pickle.loads(pickle.dumps(fresh))

        self.assertEqual(restored.schema_version, ml_model.TRAINED_MODEL_SCHEMA_VERSION)
        self.assertEqual(restored.validation_mae, {"gbrt": 1.23})
        self.assertEqual(restored.mase_scale_points, 42)
        self.assertEqual(restored.resample_minutes, 15)
        self.assertEqual(restored.training_span_days, 30.0)
