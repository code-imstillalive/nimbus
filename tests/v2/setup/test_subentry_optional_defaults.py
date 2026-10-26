"""Optional pickers in the subentry wizards take no None default, so a field
can be left empty and cleared (#339) (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import voluptuous as vol

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.const import (
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_POWER_SOURCE,
    SUBENTRY_TYPE_POWER_SOURCE,
)
from custom_components.nimbus_load.flows import (
    battery_tower_subentry,
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


# -- PV String -------------------------------------------------------------


def test_pv_string_deleted_parent_is_not_offered_back():
    # test_pv_string_deleted_parent_is_not_offered_back
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

    # test_battery_tower_deleted_parent_is_not_offered_back
    entry = _entry_with_power_sources("ps_alive")
    defaults = {CONF_BATTERY_TOWER_POWER_SOURCE: "ps_deleted"}
    markers = _markers(battery_tower_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_BATTERY_TOWER_POWER_SOURCE]) is None
    defaults[CONF_BATTERY_TOWER_POWER_SOURCE] = "ps_alive"
    markers = _markers(battery_tower_subentry._schema(defaults, entry))
    assert _suggested(markers[CONF_BATTERY_TOWER_POWER_SOURCE]) == "ps_alive"


# -- Battery Tower ---------------------------------------------------------
