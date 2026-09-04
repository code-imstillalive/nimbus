"""Regression test for nimbus issue #366 (Mark Purcell, codebase review),
the pickle-compatibility finding: _load_model_from_disk()'s own feature-
count compatibility check (`trained.x_mean.shape[0] != len(FEATURE_NAMES)`)
used to sit OUTSIDE the try/except wrapping pickle.loads() -- so a
persisted model that deserializes into something without an `x_mean`
attribute at all (a more broken/older schema than a mere feature-count
mismatch) raised a bare, uncaught AttributeError straight out of this
method, crashing async_setup() the exact "Config entry not ready" way
the method's own docstring already describes for the feature-count-
mismatch case it WAS handling.

Fixed by moving the compatibility check inside the try block, so any
shape of persisted-model incompatibility degrades to "discard and
retrain fresh," never a crash.

Real, direct construction of a bare NimbusCoordinator (same __new__
technique this project's other coordinator test files use) writing a
REAL pickle to a real temp file -- not mocking pickle.loads() itself --
so this test exercises the genuine on-disk round-trip, not just the
in-memory logic.
"""

from __future__ import annotations

import pickle
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.ml.features import FEATURE_NAMES
from custom_components.nimbus_load.ml.model import TrainedModel


def _make_coordinator(model_path: Path) -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord._model_path = model_path
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry-disk-robustness"
    return coord


def _real_trained_model() -> TrainedModel:
    n_features = len(FEATURE_NAMES)
    return TrainedModel(
        model_type="knn",
        x_mean=np.zeros(n_features),
        x_std=np.ones(n_features),
        x_train=np.zeros((5, n_features)),
        y_train=np.zeros(5),
        gbrt=None,
        trained_at=None,
        training_points=5,
        validation_mae={},
        validation_mase={},
    )


class TestLoadModelFromDiskRobustness(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._model_path = Path(self._tmpdir.name) / "model.pkl"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_file_returns_none(self):
        coord = _make_coordinator(self._model_path)
        self.assertIsNone(coord._load_model_from_disk())

    def test_a_valid_compatible_model_loads_successfully(self):
        trained = _real_trained_model()
        self._model_path.write_bytes(pickle.dumps(trained))
        coord = _make_coordinator(self._model_path)
        result = coord._load_model_from_disk()
        self.assertIsNotNone(result)
        self.assertEqual(result.model_type, "knn")

    def test_corrupt_unparseable_bytes_return_none_not_raise(self):
        self._model_path.write_bytes(b"not a real pickle at all")
        coord = _make_coordinator(self._model_path)
        self.assertIsNone(coord._load_model_from_disk())

    def test_a_wrong_feature_count_returns_none_not_raise(self):
        """The pre-existing, already-working case -- must stay working."""
        trained = _real_trained_model()
        # Simulate an older model trained under fewer features.
        object.__setattr__(trained, "x_mean", np.zeros(len(FEATURE_NAMES) - 1))
        self._model_path.write_bytes(pickle.dumps(trained))
        coord = _make_coordinator(self._model_path)
        self.assertIsNone(coord._load_model_from_disk())

    def test_a_wrong_schema_version_returns_none_not_raise(self):
        """nimbus issue #366 finding 3: a persisted model whose schema_
        version doesn't match the current code's own TRAINED_MODEL_SCHEMA_
        VERSION is a real, meaning-changing incompatibility (not just a
        feature-count mismatch) -- must discard and retrain fresh, the
        same self-healing fallback as the feature-count check above."""
        trained = _real_trained_model()
        object.__setattr__(trained, "schema_version", -1)
        self._model_path.write_bytes(pickle.dumps(trained))
        coord = _make_coordinator(self._model_path)
        self.assertIsNone(coord._load_model_from_disk())

    def test_a_pre_versioning_pickle_missing_schema_version_returns_none(self):
        """A pickle written before schema_version existed at all has no
        such key in its restored __dict__ -- TrainedModel.__setstate__
        backfills it to 0, which always mismatches any real current
        version, so this self-heals via one retrain rather than being
        silently trusted forever."""
        old = object.__new__(TrainedModel)
        old.__dict__ = {
            "model_type": "knn",
            "x_mean": np.zeros(len(FEATURE_NAMES)),
            "x_std": np.ones(len(FEATURE_NAMES)),
            "x_train": np.zeros((5, len(FEATURE_NAMES))),
            "y_train": np.zeros(5),
            "gbrt": None,
            "trained_at": None,
            "training_points": 5,
        }
        self._model_path.write_bytes(pickle.dumps(old))
        coord = _make_coordinator(self._model_path)
        self.assertIsNone(coord._load_model_from_disk())

    def test_a_pickle_missing_x_mean_entirely_returns_none_not_raise(self):
        """The real bug this issue describes: before the fix, accessing
        `trained.x_mean` on an object with no such attribute raised a
        bare AttributeError OUTSIDE the try block, crashing this method
        instead of degrading to a fresh retrain. Uses a plain
        types.SimpleNamespace (a genuinely different, more broken
        "schema" than TrainedModel with a wrong-shaped x_mean) to prove
        the fix isn't special-cased to just the shape-mismatch path."""
        broken = types.SimpleNamespace(model_type="knn")  # no x_mean at all
        self._model_path.write_bytes(pickle.dumps(broken))
        coord = _make_coordinator(self._model_path)
        # Must not raise.
        self.assertIsNone(coord._load_model_from_disk())
