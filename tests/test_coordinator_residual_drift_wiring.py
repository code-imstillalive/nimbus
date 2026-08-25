"""Real test of NimbusCoordinator._check_residual_drift() -- the actual
wiring between the anomaly layer (anomaly.py) and the coordinator, not
just the pure detect_residual_drift() math (already covered by
tests/test_anomaly_residual_drift.py).

This is the gap that mattered most to close: anomaly.py's own function
is fully tested in isolation, but nothing previously proved the
coordinator actually CALLS it correctly, actually LOGS when it fires,
actually respects the alert-fatigue flag across repeated ticks, or
actually stays silent (never raises) if the check itself breaks.

Same "construct via __new__() to bypass DataUpdateCoordinator's own
heavy __init__ chain" pattern already established in
test_coordinator_helpers.py -- only the attributes _check_residual_
drift() actually reads/writes are set, nothing else.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import NimbusCoordinator

_LOGGER_NAME = "custom_components.nimbus_load.coordinator"


def _make_bare_coordinator(title: str = "Pool Pump") -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.subentry = MagicMock(title=title)
    coord._residuals = []
    coord._residual_drift_flagged = False
    return coord


def test_stable_residuals_log_nothing(caplog):
    coord = _make_bare_coordinator()
    coord._residuals = [1.0, 1.1, 0.9, 1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 1.0] * 3
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        coord._check_residual_drift()
    assert caplog.records == []
    assert coord._residual_drift_flagged is False


def test_real_drift_logs_a_warning_naming_the_subentry(caplog):
    coord = _make_bare_coordinator(title="Whole House Load")
    baseline = [1.0, 1.1, 0.9, 1.0, 1.2, 0.8, 1.0, 1.1, 0.9, 1.0] * 2
    coord._residuals = baseline + [5.0] * 10
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        coord._check_residual_drift()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    message = caplog.records[0].getMessage()
    assert "Whole House Load" in message
    assert "drift" in message.lower()
    assert coord._residual_drift_flagged is True


def test_ongoing_drift_does_not_re_log_every_tick(caplog):
    # The actual alert-fatigue guardrail this file exists to prove: two
    # consecutive ticks with the SAME ongoing drift must log exactly
    # once, not twice.
    coord = _make_bare_coordinator()
    baseline = [1.0] * 10
    coord._residuals = baseline + [5.0] * 10
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        coord._check_residual_drift()
        coord._check_residual_drift()
    assert len(caplog.records) == 1


def test_drift_clearing_then_recurring_logs_again(caplog):
    coord = _make_bare_coordinator()
    baseline = [1.0] * 10

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Episode 1: drift starts.
        coord._residuals = baseline + [5.0] * 10
        coord._check_residual_drift()
        # Recovers: back to stable.
        coord._residuals = baseline + [1.0] * 10
        coord._check_residual_drift()
        # Episode 2: drifts again -- must log again, not stay silenced.
        coord._residuals = baseline + [5.0] * 10
        coord._check_residual_drift()

    assert len(caplog.records) == 2
    assert coord._residual_drift_flagged is True


def test_a_broken_check_logs_a_warning_and_never_raises(caplog):
    # The defensive guarantee this module's own docstring promises: the
    # anomaly layer must never break the real forecast cycle it's
    # observing, even if it breaks itself.
    coord = _make_bare_coordinator()
    coord._residuals = [1.0] * 30
    with (
        patch(
            "custom_components.nimbus_load.coordinator.detect_residual_drift",
            side_effect=RuntimeError("boom"),
        ),
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
    ):
        coord._check_residual_drift()  # must not raise
    assert len(caplog.records) == 1
    assert "failed" in caplog.records[0].getMessage().lower()


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
