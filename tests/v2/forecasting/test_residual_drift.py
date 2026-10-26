"""Residual drift: a forecaster whose recent one-step error grows well past
its own baseline is flagged, never acted on (#1837)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

anomaly = importlib.import_module("custom_components.nimbus_load.anomaly")


def test_drift_is_recent_error_over_twice_its_own_baseline():
    for residuals, watching, flagged in [
        ([0.1] * 19, False, False),  # too little history
        ([0.1] * 20, True, False),
        ([0.1] * 10 + [0.2] * 10, True, False),  # exactly twice: not yet
        ([0.1] * 10 + [0.25] * 10, True, True),
        ([0.0] * 10 + [1.0] * 10, True, False),  # a zero baseline has no ratio
    ]:
        status = anomaly.residual_drift_status(residuals)
        assert status.watching is watching and status.sample_count == len(residuals)
        found = anomaly.detect_residual_drift(residuals)
        assert (found is not None) is flagged
        if flagged:
            assert found.ratio == pytest.approx(2.5)
            assert (
                found.recent_mean_error,
                found.baseline_mean_error,
            ) == pytest.approx((0.25, 0.1))
        if watching and residuals[0] > 0:
            assert status.ratio == pytest.approx(
                sum(residuals[-10:])
                / 10
                / (sum(residuals[:-10]) / (len(residuals) - 10))
            )
