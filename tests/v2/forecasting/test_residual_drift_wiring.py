"""The coordinator's residual-drift check wired to the anomaly layer (#1837)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.coordinator import NimbusCoordinator

_LOGGER_NAME = "custom_components.nimbus_load.coordinator"


def _make_bare_coordinator(title: str = "Pool Pump") -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.subentry = MagicMock(title=title)
    coord._residuals = []
    coord._residual_drift_flagged = False
    return coord


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
