"""A bad persisted model degrades, never crashes, and an older one is served,
not discarded (#366, #373) (#1837).
"""

from __future__ import annotations

import pickle
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.ml.features import FEATURE_NAMES
from custom_components.nimbus_load.ml.model import (
    TRAINED_MODEL_SCHEMA_VERSION,
    TrainedModel,
)


def _stub(model_path: Path | None = None, residual_path: Path | None = None):
    return SimpleNamespace(
        _model_path=model_path,
        _residual_path=residual_path,
        subentry=SimpleNamespace(subentry_id="01TESTSUBENTRY"),
    )


def _real_model(**overrides) -> TrainedModel:
    n = len(FEATURE_NAMES)
    base = TrainedModel(
        model_type="knn",
        x_mean=np.zeros(n),
        x_std=np.ones(n),
        x_train=np.zeros((4, n)),
        y_train=np.zeros(4),
        gbrt=None,
        trained_at=datetime(2026, 9, 1, tzinfo=UTC),
        training_points=4,
    )
    return replace(base, **overrides) if overrides else base


class TestABadPickleDegradesInsteadOfWedgingTheIntegration:
    def test_a_missing_file_is_simply_none(self, tmp_path):
        p = tmp_path / "definitely_absent.pkl"
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None


class TestTheSchemaVersionAsymmetry:
    """#373's live lesson, and the part most at risk from a tidy-up: the
    two directions are deliberately NOT symmetric."""

    def test_an_older_schema_is_kept_and_served(self, tmp_path):
        """20+ hours of every-forecast-`unknown` on a real install came
        from discarding this. `__setstate__` has already backfilled every
        missing field, so it predicts correctly while a retrain runs."""
        p = tmp_path / "model.pkl"
        p.write_bytes(
            pickle.dumps(_real_model(schema_version=TRAINED_MODEL_SCHEMA_VERSION - 1))
        )
        loaded = NimbusCoordinator._load_model_from_disk(_stub(model_path=p))
        assert loaded is not None, (
            "an older-schema pickle was discarded -- that is exactly the "
            "'discard first, replace later' behaviour #373 was filed about, "
            "which leaves a subentry with nothing the moment a retrain fails"
        )


class TestTheResidualBufferAlsoDegrades:
    @pytest.mark.parametrize(
        "content",
        [
            "not json at all",
            '{"not": "a list"}',
            '["strings", "not", "numbers"]',
            "[1.0, 2.0, null]",
        ],
    )
    def test_unusable_content_starts_fresh(self, tmp_path, content):
        p = tmp_path / "residuals.json"
        p.write_text(content, encoding="utf-8")
        assert NimbusCoordinator._load_residuals_from_disk(_stub(residual_path=p)) == []

    def test_a_missing_file_starts_fresh(self, tmp_path):
        p = tmp_path / "absent.json"
        assert NimbusCoordinator._load_residuals_from_disk(_stub(residual_path=p)) == []
