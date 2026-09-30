"""nimbus issue #1475: a forecast refresh racing Home Assistant's shutdown
reached the recorder after its executor stopped and logged
"cannot schedule new futures after shutdown" as an ERROR on a routine restart
(observed on devhub, 2026-09-30). While HA is stopping the coordinator now
keeps its current data and does not fetch.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _coordinator(*, stopping):
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord.hass.is_stopping = stopping
    coord.data = {"state": 1.23, "forecast": [1, 2, 3]}
    # A trained model would go on to fetch history; make that fetch explode
    # the way the real recorder does after shutdown.
    coord._trained = MagicMock()
    coord._async_fetch_recorder_history = AsyncMock(
        side_effect=RuntimeError("cannot schedule new futures after shutdown")
    )
    return coord


def test_a_refresh_while_stopping_keeps_current_data_and_does_not_fetch():
    coord = _coordinator(stopping=True)
    result = asyncio.run(coord._async_update_data())
    assert result == {"state": 1.23, "forecast": [1, 2, 3]}
    coord._async_fetch_recorder_history.assert_not_called()


def test_a_test_double_is_stopping_attribute_does_not_trip_the_guard():
    """MagicMock auto-attributes are truthy; only a real `True` may skip."""
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()  # is_stopping is a MagicMock, not True
    coord._trained = None
    coord.subentry = MagicMock()
    coord.subentry.data = {}
    coord._last_retrain_error = None
    from dataclasses import dataclass

    @dataclass
    class _Drift:
        flagged: bool = False

    coord._residual_drift_status = _Drift()
    result = asyncio.run(coord._async_update_data())
    assert result["training_points"] == 0, "must take the normal untrained path"


def test_not_stopping_takes_the_normal_path_not_the_early_return():
    """With is_stopping False the method must compute (here: the untrained
    branch), never hand back the existing data unchanged."""
    from dataclasses import dataclass

    @dataclass
    class _Drift:
        flagged: bool = False

    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord.hass.is_stopping = False
    coord.data = {"sentinel": "existing"}
    coord._trained = None
    coord.subentry = MagicMock()
    coord.subentry.data = {}
    coord._last_retrain_error = None
    coord._residual_drift_status = _Drift()
    result = asyncio.run(coord._async_update_data())
    assert result != {"sentinel": "existing"}
    assert result["training_points"] == 0
