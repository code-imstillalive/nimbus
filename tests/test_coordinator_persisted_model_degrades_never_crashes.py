"""nimbus issues #366 / #373: a bad persisted model must degrade, never
crash — and an *older* one must be served, not discarded.

Found by auditing `except` guards for test coverage. Both loaders below
sit on the Forecaster's cold-start path, both carry real incident history
in their comments, and neither had a test.

The failure mode is unusually bad, which is why it is worth pinning.
From `_load_model_from_disk()`'s own comment:

    Confirmed live 2026-08-15: loading an incompatible model straight
    into predict() raises a raw numpy broadcast ValueError deep inside an
    executor thread, which HA surfaces as an opaque "Config entry not
    ready yet" retry loop with no obvious fix -- every retry hits the
    same crash since the stale pickle never gets replaced on its own.

A crash here does not degrade the Forecaster, it **wedges the whole
integration permanently**: the retry can never succeed, because nothing
ever replaces the file causing it.

#366 (Mark Purcell) then found the feature-count check itself sitting
*outside* the `try`, so a pickle that deserialized into something without
an `x_mean` attribute raised a bare `AttributeError` straight out of the
method — the same wedged outcome, for an even more broken pickle than the
check was written for.

#373 is the opposite lesson, from a second live incident:

    "discard first, replace later" means a subentry has genuinely
    NOTHING the moment that retrain fails for any reason (confirmed
    live: the #372 LTS float-epoch crash turned one version bump into a
    20+ hour, every-forecast-`unknown` outage on a real install)

So an older-schema pickle is deliberately **kept and served** while a
retrain runs, and only a *newer* one is discarded. That asymmetry is
subtle, easy to "simplify" away, and cost a real household 20 hours — so
it gets its own test.

Called as unbound methods against a minimal stub `self`. These loaders
touch only `self._model_path` / `self._residual_path` and (for logging)
`self.subentry.subentry_id`, so building a real `DataUpdateCoordinator`
would add scaffolding without adding coverage.
"""

from __future__ import annotations

import json
import pickle
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    def test_garbage_bytes_return_none_rather_than_raising(self, tmp_path):
        p = tmp_path / "model.pkl"
        p.write_bytes(b"this is not a pickle at all")
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None

    def test_a_pickle_without_x_mean_returns_none_rather_than_raising(self, tmp_path):
        """#366 finding 3, exactly: this used to raise a bare
        AttributeError out of the method, because the feature-count check
        sat outside the `try`."""
        p = tmp_path / "model.pkl"
        p.write_bytes(pickle.dumps({"not": "a TrainedModel"}))
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None

    def test_a_feature_count_mismatch_is_discarded(self, tmp_path):
        """The 2026-08-15 incident: a stale pickle whose feature count no
        longer matches would otherwise blow up inside predict()."""
        p = tmp_path / "model.pkl"
        short = len(FEATURE_NAMES) - 1
        p.write_bytes(
            pickle.dumps(_real_model(x_mean=np.zeros(short), x_std=np.ones(short)))
        )
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None

    def test_a_missing_file_is_simply_none(self, tmp_path):
        p = tmp_path / "definitely_absent.pkl"
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None

    def test_a_healthy_model_is_returned(self, tmp_path):
        """Guards every assertion above from passing because the loader
        returns None unconditionally."""
        p = tmp_path / "model.pkl"
        p.write_bytes(pickle.dumps(_real_model()))
        loaded = NimbusCoordinator._load_model_from_disk(_stub(model_path=p))
        assert loaded is not None
        assert loaded.model_type == "knn"


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

    def test_a_newer_schema_is_discarded(self, tmp_path):
        """The other direction is a real downgrade: this code has no
        backfill for a schema it has never seen."""
        p = tmp_path / "model.pkl"
        p.write_bytes(
            pickle.dumps(_real_model(schema_version=TRAINED_MODEL_SCHEMA_VERSION + 1))
        )
        assert NimbusCoordinator._load_model_from_disk(_stub(model_path=p)) is None


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

    def test_a_healthy_buffer_is_returned_as_floats(self, tmp_path):
        """Baseline, so the parametrised cases above cannot pass because
        the loader always returns []."""
        p = tmp_path / "residuals.json"
        p.write_text(json.dumps([1, 2.5, -3]), encoding="utf-8")
        out = NimbusCoordinator._load_residuals_from_disk(_stub(residual_path=p))
        assert out == [1.0, 2.5, -3.0]
        assert all(isinstance(v, float) for v in out)
