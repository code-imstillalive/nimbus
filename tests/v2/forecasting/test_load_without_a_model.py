"""A deterministic load publishes its forecast before any model has trained
(#1575) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import (
    CONF_EXPECTED_LOAD_KW,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_LOAD_SENSOR,
    CONF_SCHEDULE_END_HOUR,
    CONF_SCHEDULE_START_HOUR,
    RESAMPLE_MINUTES,
    SUBENTRY_TYPE_LOAD,
)
from custom_components.nimbus_load.coordinator import (
    NimbusCoordinator,
)
from custom_components.nimbus_load.ml.model import deterministic_values, predict

# 02:00 UTC is 12:00 in Brisbane (UTC+10, no DST): inside an 08:00-15:00
# window, so the first published value is the expected kW.
NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
LOCAL_TZ = timezone(timedelta(hours=10))


@dataclass
class _FakeResidualDriftStatus:
    status: str = "ok"


def _coordinator(data: dict) -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord.hass.is_stopping = False
    coord.entry = SimpleNamespace(options={CONF_FORECAST_HORIZON_HOURS: 96})
    coord.subentry = SimpleNamespace(
        subentry_id="pool",
        subentry_type=SUBENTRY_TYPE_LOAD,
        data={CONF_LOAD_SENSOR: "sensor.pool_pump_power", **data},
    )
    coord._trained = None
    coord._last_retrain_error = None
    coord._residual_drift_status = _FakeResidualDriftStatus()
    return coord


def _run(coord: NimbusCoordinator) -> dict:
    with (
        patch.object(coordinator_module.dt_util, "utcnow", return_value=NOW),
        patch.object(
            coordinator_module.dt_util,
            "as_local",
            side_effect=lambda t: t.astimezone(LOCAL_TZ),
        ),
    ):
        return asyncio.run(coord._async_update_data())


def _in_window(iso: str, start: float, end: float) -> bool:
    ts = datetime.fromisoformat(iso)
    hour = ts.hour + ts.minute / 60.0
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


DETERMINISTIC = {
    CONF_EXPECTED_LOAD_KW: 1.3,
    CONF_SCHEDULE_START_HOUR: "08:00:00",
    CONF_SCHEDULE_END_HOUR: "15:00:00",
}


def test_matches_what_predict_returns_for_the_same_timestamps():
    """The no-model path and predict()'s deterministic branch share one
    helper, so the values cannot drift apart."""
    forecast = _run(_coordinator(DETERMINISTIC))["forecast"]
    timestamps = [datetime.fromisoformat(p["time"]) for p in forecast]
    via_predict = predict(
        None,  # the deterministic branch never reads the model
        timestamps,
        [22.0] * len(timestamps),
        [50.0] * len(timestamps),
        [],
        RESAMPLE_MINUTES,
        schedule_start_hour=8.0,
        schedule_end_hour=15.0,
        expected_load_kw=1.3,
    ).values
    assert via_predict == deterministic_values(timestamps, 1.3, 8.0, 15.0)
    assert [p["value"] for p in forecast] == [round(v, 3) for v in via_predict]


def test_overnight_window_wraps():
    coord = _coordinator(
        {
            CONF_EXPECTED_LOAD_KW: 2.0,
            CONF_SCHEDULE_START_HOUR: "22:00:00",
            CONF_SCHEDULE_END_HOUR: "06:00:00",
        }
    )
    for point in _run(coord)["forecast"]:
        expected = 2.0 if _in_window(point["time"], 22.0, 6.0) else 0.0
        assert point["value"] == expected, point


# --- Mark's review of #1593: readiness and provenance ---------------------------


def test_configured_zero_incomplete_rule_and_missing_history_are_distinct():
    zero = _run(
        _coordinator(
            {
                CONF_EXPECTED_LOAD_KW: 0.0,
                CONF_SCHEDULE_START_HOUR: "08:00:00",
                CONF_SCHEDULE_END_HOUR: "15:00:00",
            }
        )
    )
    assert (zero["forecast_origin"], zero["forecast_readiness"]) == (
        "configured_rule",
        "ready",
    )
    assert zero["forecast"] and {p["value"] for p in zero["forecast"]} == {0.0}
    incomplete = _run(_coordinator({CONF_EXPECTED_LOAD_KW: 1.3}))
    assert incomplete["forecast_readiness"] == "incomplete_rule"
    assert incomplete["forecast"] == []
    for data in (
        {},
        {CONF_SCHEDULE_START_HOUR: "08:00:00", CONF_SCHEDULE_END_HOUR: "15:00:00"},
    ):
        missing = _run(_coordinator(data))
        assert missing["forecast_readiness"] == "not_trained", data
        assert missing["forecast_origin"] is None
