"""nimbus issue #1575: a deterministic load publishes its forecast before any
model has trained.

A load with expected_load_kw and both schedule hours set is in the
deterministic mode: predict() returns expected_load_kw inside the window and
0.0 outside it, and never reads the trained model. The coordinator used to
return the empty untrained payload whenever no model existed, before the mode
was consulted, so a load the user had fully specified published nothing until
an unrelated model trained.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    forecast_provenance,
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


def test_deterministic_load_publishes_a_forecast_without_a_trained_model():
    coord = _coordinator(DETERMINISTIC)
    hours = coord._horizon_hours
    result = _run(coord)

    assert result["mode"] == "deterministic"
    forecast = result["forecast"]
    assert len(forecast) == hours * 60 // RESAMPLE_MINUTES + 1
    for point in forecast:
        assert set(point) == {"time", "value", "lower", "upper"}
        expected = 1.3 if _in_window(point["time"], 8.0, 15.0) else 0.0
        assert point["value"] == expected, point
        assert point["lower"] == point["upper"] == expected
    assert {p["value"] for p in forecast} == {0.0, 1.3}
    assert result["state"] == forecast[0]["value"] == 1.3
    assert result["training_points"] == 0
    assert result["model_type"] is None
    assert result["trained_at"] is None


def test_grid_is_resample_spaced_from_now():
    forecast = _run(_coordinator(DETERMINISTIC))["forecast"]
    times = [datetime.fromisoformat(p["time"]) for p in forecast]
    assert times[0].astimezone(UTC) == NOW
    for a, b in pairwise(times):
        assert b - a == timedelta(minutes=RESAMPLE_MINUTES)


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


def test_other_modes_still_return_the_empty_untrained_payload():
    for data in (
        {},  # unscheduled
        {  # scheduled_ml: a window but no expected kW
            CONF_SCHEDULE_START_HOUR: "08:00:00",
            CONF_SCHEDULE_END_HOUR: "15:00:00",
        },
        {CONF_EXPECTED_LOAD_KW: 1.3},  # expected kW alone is not deterministic
    ):
        result = _run(_coordinator(data))
        assert result["mode"] != "deterministic", data
        assert result["state"] is None, data
        assert result["forecast"] == [], data
        assert result["training_points"] == 0, data


# --- Mark's review of #1593: readiness and provenance ---------------------------


def test_provenance_table():
    """Every combination, so a configured rule, a learned model, an
    incomplete rule and missing history are always told apart."""
    assert forecast_provenance("deterministic", False, True) == (
        "configured_rule",
        "ready",
    )
    assert forecast_provenance("deterministic", True, True) == (
        "configured_rule",
        "ready",
    )
    assert forecast_provenance("scheduled_ml", True, False) == ("learned", "ready")
    assert forecast_provenance("unscheduled", True, True) == ("learned", "ready")
    assert forecast_provenance("unscheduled", False, True) == (None, "incomplete_rule")
    assert forecast_provenance("scheduled_ml", False, False) == (None, "not_trained")
    assert forecast_provenance("unscheduled", False, False) == (None, "not_trained")


def test_a_complete_rule_with_no_model_is_ready_from_the_configured_rule():
    result = _run(_coordinator(DETERMINISTIC))
    assert result["forecast_origin"] == "configured_rule"
    assert result["forecast_readiness"] == "ready"
    assert result["trained_at"] is None  # no ML evidence is fabricated


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


def test_an_out_of_window_zero_is_a_rule_value_not_missing_evidence():
    result = _run(_coordinator(DETERMINISTIC))
    outside = [p for p in result["forecast"] if not _in_window(p["time"], 8.0, 15.0)]
    assert outside and all(p["value"] == 0.0 for p in outside)
    assert result["forecast_origin"] == "configured_rule"


def test_window_start_is_inclusive_and_end_exclusive_on_local_time():
    def at(h, m=0):
        return datetime(2026, 10, 6, h, m, tzinfo=LOCAL_TZ)

    stamps = [at(7, 55), at(8, 0), at(14, 55), at(15, 0)]
    assert deterministic_values(stamps, 1.3, 8.0, 15.0) == [0.0, 1.3, 1.3, 0.0]
    # Overnight: 22:00 starts it, 06:00 ends it.
    stamps = [at(21, 55), at(22, 0), at(5, 55), at(6, 0)]
    assert deterministic_values(stamps, 2.0, 22.0, 6.0) == [0.0, 2.0, 2.0, 0.0]


def test_the_window_follows_the_sites_wall_clock_not_utc():
    """The same UTC instant is inside an 08:00-15:00 window in Brisbane
    (12:00 local) and outside it in UTC (02:00)."""
    instant = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
    assert deterministic_values([instant.astimezone(LOCAL_TZ)], 1.3, 8.0, 15.0) == [1.3]
    assert deterministic_values([instant], 1.3, 8.0, 15.0) == [0.0]


def test_switching_modes_switches_provenance():
    """With a model on disk, a complete rule still wins (predict() returns
    it first), and removing the rule makes the forecast learned again --
    a scheduled output is never mistaken for a trained one, or vice versa."""
    with_model = True
    assert (
        forecast_provenance("deterministic", with_model, True)[0] == "configured_rule"
    )
    assert forecast_provenance("scheduled_ml", with_model, False)[0] == "learned"
    assert forecast_provenance("scheduled_ml", False, False) == (None, "not_trained")
