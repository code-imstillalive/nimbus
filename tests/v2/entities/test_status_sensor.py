"""A plain-language status: not yet configured, learning, needs attention or
working well, in that order (#450) (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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
