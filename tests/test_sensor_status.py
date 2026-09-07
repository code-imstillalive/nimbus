"""Tests for nimbus issue #450 (Mark Purcell, sub-issue of #448):
NimbusStatusSensor -- a single, plain-language "is this working?"
answer in front of the 13+ jargon-carrying sensors, reading their
already-published live states rather than duplicating any scoring
logic. Confirms each of the four documented states (Not yet
configured / Learning / Needs attention / Working well) fires under
the exact real condition #450's own issue body describes, checked in
priority order, and that a missing sibling sensor degrades gracefully
rather than crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor


def _fake_entry(entry_id: str = "test-entry-abc") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _fake_state(state, attributes=None):
    return SimpleNamespace(state=state, attributes=attributes or {})


def _construct(states: dict) -> sensor.NimbusStatusSensor:
    entry = _fake_entry()
    instance = sensor.NimbusStatusSensor.__new__(sensor.NimbusStatusSensor)
    sensor.NimbusStatusSensor.__init__(instance, entry, sw_version="0.94.146")
    fake_hass = MagicMock()
    fake_hass.states.get.side_effect = lambda entity_id: states.get(entity_id)
    instance.hass = fake_hass
    return instance


class TestNotYetConfigured:
    def test_missing_config_sensor(self):
        entity = _construct({})
        assert entity.native_value == "Not yet configured"

    def test_config_sensor_not_configured_state(self):
        entity = _construct(
            {"sensor.nimbus_solver_config": _fake_state("unconfigured")}
        )
        assert entity.native_value == "Not yet configured"
        assert "wizard" in entity.extra_state_attributes["reason"].lower()


class TestLearning:
    def test_never_trained_subentries_take_priority_over_healthy_errors(self):
        entity = _construct(
            {
                "sensor.nimbus_solver_config": _fake_state("configured"),
                "sensor.nimbus_health_report": _fake_state(
                    "0",
                    {"never_trained": [{"subentry_id": "abc", "title": "Pool Power"}]},
                ),
            }
        )
        assert entity.native_value == "Learning"
        assert "Pool Power" in entity.extra_state_attributes["reason"]

    def test_more_than_three_never_trained_shows_a_plus_count(self):
        never_trained = [
            {"subentry_id": str(i), "title": f"Load {i}"} for i in range(5)
        ]
        entity = _construct(
            {
                "sensor.nimbus_solver_config": _fake_state("configured"),
                "sensor.nimbus_health_report": _fake_state(
                    "0", {"never_trained": never_trained}
                ),
            }
        )
        reason = entity.extra_state_attributes["reason"]
        assert "+2 more" in reason


class TestNeedsAttention:
    def test_real_health_report_errors(self):
        entity = _construct(
            {
                "sensor.nimbus_solver_config": _fake_state("configured"),
                "sensor.nimbus_health_report": _fake_state("3", {}),
            }
        )
        assert entity.native_value == "Needs attention"
        assert "3 recent error" in entity.extra_state_attributes["reason"]

    def test_non_optimal_solver_plan_status(self):
        entity = _construct(
            {
                "sensor.nimbus_solver_config": _fake_state("configured"),
                "sensor.nimbus_health_report": _fake_state("0", {}),
                "sensor.nimbus_solver_battery_forecast": _fake_state(
                    "-4.6", {"status": "infeasible"}
                ),
            }
        )
        assert entity.native_value == "Needs attention"
        assert "infeasible" in entity.extra_state_attributes["reason"]


class TestWorkingWell:
    def test_all_healthy(self):
        entity = _construct(
            {
                "sensor.nimbus_solver_config": _fake_state("configured"),
                "sensor.nimbus_health_report": _fake_state("0", {}),
                "sensor.nimbus_solver_battery_forecast": _fake_state(
                    "-4.6", {"status": "optimal"}
                ),
            }
        )
        assert entity.native_value == "Working well"

    def test_missing_sibling_sensors_degrade_gracefully_not_crash(self):
        # Only the config sensor exists (a fresh install where the
        # Solver's own push sensors haven't fired yet) -- must not
        # raise, must fall through to a real, honest answer.
        entity = _construct({"sensor.nimbus_solver_config": _fake_state("configured")})
        assert entity.native_value == "Working well"


class TestPresentationLayerOnly:
    def test_extra_state_attributes_always_includes_reason_and_generated_at(self):
        entity = _construct({})
        attrs = entity.extra_state_attributes
        assert "reason" in attrs
        assert "generated_at" in attrs

    def test_entity_id_and_unique_id(self):
        entity = _construct({})
        assert entity.entity_id == "sensor.nimbus_status"
        assert entity._attr_unique_id == "test-entry-abc_status"
