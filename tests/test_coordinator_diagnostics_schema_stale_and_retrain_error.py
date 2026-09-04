"""Regression test for nimbus issue #373 (Mark Purcell, codebase review):
the only operator-facing signal a retrain failure or a schema-stale
served model had before this was a log line -- last_update_success stays
True regardless (the coordinator's own update succeeded; it's a separate
background retrain task that failed, or an old-but-serving model that's
quietly out of date). Both are now published as diagnostic attributes on
the forecast entity itself.

Covers the untrained branch of _async_update_data() directly (simple,
self-contained); the "trained" branch's own model_schema_stale/
last_retrain_error keys are a direct, one-line attribute read with no
independent logic of their own -- see test_coordinator_load_model_from_
disk_robustness.py and test_coordinator_retrain_failure_visibility.py
for the real coverage of the values feeding them.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import NimbusCoordinator


@dataclass
class _FakeResidualDriftStatus:
    # _async_update_data() calls dataclasses.asdict() on this -- needs a
    # real dataclass instance, not a MagicMock (asdict() raises TypeError
    # on a non-dataclass).
    flagged: bool = False


def _make_untrained_coordinator(last_retrain_error: str | None) -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord._trained = None
    # _mode is a read-only @property computed from subentry.data -- an
    # empty dict resolves every schedule field to None, giving "unscheduled".
    coord.subentry = MagicMock()
    coord.subentry.data = {}
    coord._last_retrain_error = last_retrain_error
    coord._residual_drift_status = _FakeResidualDriftStatus()
    return coord


def test_untrained_dict_reports_no_schema_stale_flag():
    coord = _make_untrained_coordinator(last_retrain_error=None)
    result = asyncio.run(coord._async_update_data())
    assert result["model_schema_stale"] is False


def test_untrained_dict_surfaces_a_pending_last_retrain_error():
    coord = _make_untrained_coordinator(last_retrain_error="RuntimeError: boom")
    result = asyncio.run(coord._async_update_data())
    assert result["last_retrain_error"] == "RuntimeError: boom"


def test_untrained_dict_last_retrain_error_is_none_when_nothing_has_failed():
    coord = _make_untrained_coordinator(last_retrain_error=None)
    result = asyncio.run(coord._async_update_data())
    assert result["last_retrain_error"] is None
