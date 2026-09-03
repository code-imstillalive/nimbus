"""nimbus issue #339: the Power Source, PV String and Battery Tower
subentry flows built their optional pickers as
`vol.Optional(key, default=defaults.get(key))`. For a fresh add that is
`default=None`, which voluptuous injects on omission and HA's
EntitySelector/SelectSelector then reject ("Entity None is neither a
valid entity ID nor a valid UUID" / "expected str") -- so a PV-only
Power Source could not be created, a set sensor could never be cleared
(the saved default was re-injected), and a PV String / Battery Tower
whose parent Power Source had been deleted could never be reconfigured
("value must be one of []").

The fix is the same suggested_value pattern hub_options.py already uses
(#113/#114). These tests pin the schema SHAPE, since the stub selectors
here accept anything: no marker may carry a None default, and a stale
parent reference must not be offered back.

Runs against real voluptuous + tests/_ha_stubs.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import voluptuous as vol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_POWER_SOURCE,
    SUBENTRY_TYPE_POWER_SOURCE,
)
from custom_components.nimbus_load.flows import (
    battery_tower_subentry,
    power_source_subentry,
    pv_string_subentry,
)


def _markers(schema: vol.Schema) -> dict[str, vol.Marker]:
    return {str(m): m for m in schema.schema}


def _entry_with_power_sources(*ids: str) -> MagicMock:
    entry = MagicMock()
    entry.subentries = {
        sid: MagicMock(
            subentry_id=sid, subentry_type=SUBENTRY_TYPE_POWER_SOURCE, title=sid
        )
        for sid in ids
    }
    return entry


def _suggested(marker: vol.Marker):
    return (marker.description or {}).get("suggested_value")


# -- Power Source ----------------------------------------------------------


def test_power_source_fresh_add_injects_no_none_defaults():
    markers = _markers(power_source_subentry._schema({}))
    for key in (CONF_POWER_SOURCE_BATTERY_SENSOR, CONF_POWER_SOURCE_DC_SENSOR):
        assert markers[key].default is vol.UNDEFINED
        assert _suggested(markers[key]) is None
    # A PV-only source: name only, both pickers blank, must validate.
    out = power_source_subentry._schema({})({CONF_POWER_SOURCE_NAME: "SolarEdge"})
    assert out == {CONF_POWER_SOURCE_NAME: "SolarEdge"}


def test_power_source_saved_sensor_is_suggested_not_forced():
    defaults = {
        CONF_POWER_SOURCE_NAME: "Inverter",
        CONF_POWER_SOURCE_BATTERY_SENSOR: "sensor.batt",
    }
    markers = _markers(power_source_subentry._schema(defaults))
    assert markers[CONF_POWER_SOURCE_BATTERY_SENSOR].default is vol.UNDEFINED
    assert _suggested(markers[CONF_POWER_SOURCE_BATTERY_SENSOR]) == "sensor.batt"
    # Clearing the picker (key omitted) must NOT re-inject the old value.
    out = power_source_subentry._schema(defaults)({CONF_POWER_SOURCE_NAME: "Inverter"})
    assert CONF_POWER_SOURCE_BATTERY_SENSOR not in out


# -- PV String -------------------------------------------------------------


def test_pv_string_fresh_add_with_no_power_sources_validates():
    entry = _entry_with_power_sources()  # none configured yet
    schema = pv_string_subentry._schema({}, entry)
    markers = _markers(schema)
    assert markers[CONF_PV_STRING_ENTITY].default is vol.UNDEFINED
    assert markers[CONF_PV_STRING_POWER_SOURCE].default is vol.UNDEFINED
    assert schema({CONF_PV_STRING_ENTITY: "sensor.pv_west"}) == {
        CONF_PV_STRING_ENTITY: "sensor.pv_west",
        "pv_string_label": "",
    }


def test_pv_string_deleted_parent_is_not_offered_back():
    entry = _entry_with_power_sources("ps_alive")
    defaults = {
        CONF_PV_STRING_ENTITY: "sensor.pv",
        CONF_PV_STRING_POWER_SOURCE: "ps_deleted",
    }
    markers = _markers(pv_string_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_PV_STRING_POWER_SOURCE]) is None
    # ...while a still-present parent is suggested unchanged.
    defaults[CONF_PV_STRING_POWER_SOURCE] = "ps_alive"
    markers = _markers(pv_string_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_PV_STRING_POWER_SOURCE]) == "ps_alive"
    assert markers[CONF_PV_STRING_ENTITY].default() == "sensor.pv"


# -- Battery Tower ---------------------------------------------------------


def test_battery_tower_partially_filled_validates_and_has_no_none_defaults():
    entry = _entry_with_power_sources()
    schema = battery_tower_subentry._schema({}, entry)
    for marker in schema.schema:
        assert marker.default is vol.UNDEFINED, str(marker)
    assert schema({CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.soc"}) == {
        CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.soc"
    }
    assert schema({}) == {}


def test_battery_tower_deleted_parent_is_not_offered_back():
    entry = _entry_with_power_sources("ps_alive")
    defaults = {CONF_BATTERY_TOWER_POWER_SOURCE: "ps_deleted"}
    markers = _markers(battery_tower_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_BATTERY_TOWER_POWER_SOURCE]) is None
    defaults[CONF_BATTERY_TOWER_POWER_SOURCE] = "ps_alive"
    markers = _markers(battery_tower_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_BATTERY_TOWER_POWER_SOURCE]) == "ps_alive"
