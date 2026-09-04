"""Real test of NimbusHealthReportSensor (2026-08-25) -- the always-on
"what's failing, what's flatlined, what's not running" bridge sensor.
Same pattern as test_sensor_topology_config.py: exercises the REAL
class against tests/_ha_stubs.py's stand-in homeassistant.* modules,
plus the real (non-HA-dependent) health.py log buffer.
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import health, sensor
from custom_components.nimbus_load.const import SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL


def _fake_subentry(subentry_id: str, subentry_type: str, title: str) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = subentry_type
    s.title = title
    return s


def _fake_coordinator(data: dict | None) -> MagicMock:
    c = MagicMock()
    c.data = data
    return c


def _fake_entry(subentries: dict, runtime_data: dict) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.subentries = subentries
    entry.runtime_data = runtime_data
    return entry


def _reset_log_buffer():
    health.reset_log_buffer_for_tests()
    health._handler_installed = False
    logging.getLogger(health._LOGGER_NAMESPACE).handlers.clear()
    health.install_log_buffer_handler()


def test_entity_id_and_unique_id_are_fixed_one_per_hub():
    entry = _fake_entry({}, {})
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    assert s.entity_id == "sensor.nimbus_health_report"
    assert s._attr_unique_id == "test_entry_health_report"


def test_native_value_is_error_count_from_the_log_buffer():
    _reset_log_buffer()
    logger = logging.getLogger(f"{health._LOGGER_NAMESPACE}.coordinator")
    logger.error("a real error")
    logger.warning("a real warning, not counted here")
    entry = _fake_entry({}, {})
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    assert s.native_value == 1


def test_never_trained_subentry_is_flagged():
    subentries = {
        "load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1"),
    }
    runtime_data = {
        "load1": _fake_coordinator({"trained_at": None, "training_points": 0})
    }
    entry = _fake_entry(subentries, runtime_data)
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert len(attrs["never_trained"]) == 1
    assert attrs["never_trained"][0]["subentry_id"] == "load1"
    assert attrs["never_trained"][0]["title"] == "HWS L1"


def test_trained_subentry_is_not_flagged_as_never_trained():
    subentries = {
        "load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1"),
    }
    runtime_data = {
        "load1": _fake_coordinator(
            {"trained_at": "2026-08-25T00:00:00+10:00", "training_points": 600}
        )
    }
    entry = _fake_entry(subentries, runtime_data)
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["never_trained"] == []


def test_subentry_status_includes_every_forecastable_subentry():
    subentries = {
        "load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1"),
        "sig1": _fake_subentry("sig1", SUBENTRY_TYPE_SIGNAL, "Battery"),
    }
    runtime_data = {
        "load1": _fake_coordinator(
            {"trained_at": "t", "training_points": 500, "forecast": [{"time": "t"}]}
        ),
        "sig1": _fake_coordinator(None),
    }
    entry = _fake_entry(subentries, runtime_data)
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    by_id = {row["subentry_id"]: row for row in attrs["subentry_status"]}
    assert by_id["load1"]["training_points"] == 500
    assert by_id["load1"]["forecast_point_count"] == 1
    # A coordinator whose .data is genuinely None (never updated at all)
    # must degrade to honest defaults, not crash.
    assert by_id["sig1"]["training_points"] == 0
    assert by_id["sig1"]["model_trained_at"] is None


def test_missing_coordinator_for_a_subentry_does_not_crash():
    # A real, transient state right after a subentry is added but before
    # its coordinator has finished setup -- entry.runtime_data may not
    # have an entry for it yet.
    subentries = {"load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1")}
    entry = _fake_entry(subentries, {})
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["subentry_status"][0]["training_points"] == 0
    assert len(attrs["never_trained"]) == 1


def test_residual_drift_status_passes_through_from_coordinator_data():
    # 2026-08-25, nimbus issue #187 (Mark Purcell, real-install ask): a
    # positive "am I watching, current ratio" signal must be visible on
    # the health report, not just a silent WARNING that only appears
    # once something is already wrong.
    watching_status = {
        "watching": True,
        "sample_count": 42,
        "recent_mean_error": 1.3,
        "baseline_mean_error": 1.0,
        "ratio": 1.3,
    }
    subentries = {"load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1")}
    runtime_data = {
        "load1": _fake_coordinator(
            {
                "trained_at": "t",
                "training_points": 500,
                "forecast": [],
                "residual_drift_status": watching_status,
            }
        )
    }
    entry = _fake_entry(subentries, runtime_data)
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["subentry_status"][0]["residual_drift_status"] == watching_status


def test_residual_drift_status_is_none_when_coordinator_data_is_missing():
    # Cold-start / never-updated coordinator -- must degrade to an
    # honest None, not crash or fabricate a fake "watching" value.
    subentries = {"load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, "HWS L1")}
    entry = _fake_entry(subentries, {"load1": _fake_coordinator(None)})
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["subentry_status"][0]["residual_drift_status"] is None


def test_recent_errors_and_warnings_are_exposed_separately():
    _reset_log_buffer()
    logger = logging.getLogger(f"{health._LOGGER_NAMESPACE}.coordinator")
    logger.error("a real error")
    logger.warning("a real warning")
    entry = _fake_entry({}, {})
    s = sensor.NimbusHealthReportSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert len(attrs["recent_errors"]) == 1
    assert attrs["recent_errors"][0]["level"] == "ERROR"
    assert len(attrs["recent_warnings"]) == 2  # WARNING threshold includes ERROR too
