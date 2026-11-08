"""#2392 test-before-configure (Mark Purcell, 8 Nov 2026), on real Home
Assistant: a wizard field naming an entity that does not exist is refused;
one that exists but is not reporting yet is accepted.

Drives the real Load subentry flow, then the shared helper on a schema with
a collapsed section, the shape the Solver wizard uses.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
)
from custom_components.nimbus_load.flows import missing_entity_errors


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (entity not found)", data={}, options={}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_wizard_refuses_an_entity_that_does_not_exist(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
) -> None:
    # The real Load form: a typo'd sensor is refused on its own field, and
    # nothing is created.
    result = await hass.config_entries.subentries.async_init(
        (nimbus_entry.entry_id, SUBENTRY_TYPE_LOAD), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOAD_SENSOR: "sensor.house_lod_power"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_LOAD_SENSOR: "entity_not_found"}
    assert not nimbus_entry.subentries

    # The shared helper, through a collapsed section as the Solver wizard
    # nests its optional fields.
    hass.states.async_set("sensor.not_reporting", "unavailable")
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "registered_only", suggested_object_id="registered_only"
    )
    schema = vol.Schema(
        {
            vol.Optional("a"): selector.EntitySelector(),
            vol.Optional("advanced"): section(
                vol.Schema(
                    {
                        vol.Optional("b"): selector.EntitySelector(),
                        vol.Optional("c"): selector.EntitySelector(
                            selector.EntitySelectorConfig(multiple=True)
                        ),
                    }
                )
            ),
        }
    )
    errors = missing_entity_errors(
        hass,
        schema,
        {
            # exists but is not reporting yet: accepted
            "a": "sensor.not_reporting",
            "advanced": {
                # in the registry but not in the state machine: accepted
                "b": "sensor.registered_only",
                # one of several does not exist: refused
                "c": ["sensor.not_reporting", "sensor.nowhere"],
            },
        },
    )
    assert errors == {"c": "entity_not_found"}
