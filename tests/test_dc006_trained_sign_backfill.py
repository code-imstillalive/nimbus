"""DC-006 slice 2 (#1574): `TrainedModel.power_feature_sign`.

A model persisted before the field existed was trained in `legacy`, on each
sensor's own sign, so it must restore as "native"; a fresh model defaults to
"native" too, and only a retrain while the device setup is `active` stamps
"canonical" (coordinator._async_retrain)."""

from __future__ import annotations

import _ml_path  # noqa: F401
import numpy as np
from nimbus_load.ml import model as ml_model


def _model() -> ml_model.TrainedModel:
    from datetime import UTC, datetime

    return ml_model.TrainedModel(
        model_type="naive",
        x_mean=np.zeros(3),
        x_std=np.ones(3),
        x_train=np.zeros((1, 3)),
        y_train=np.zeros(1),
        gbrt=None,
        trained_at=datetime(2026, 10, 21, tzinfo=UTC),
        training_points=1,
    )


def test_a_fresh_model_is_native():
    assert _model().power_feature_sign == "native"
