"""Real test of NimbusTopologyConfigSensor (2026-08-23) -- the bridge
sensor exposing Power Source / PV String / Battery Tower subentries
plus the switchboard's own hub-level options, out to a plain sensor
the topology dashboard card can actually read (config_entries.
subentries/.options aren't exposed via HA's plain REST API, same root
constraint NimbusSolverConfigSensor already documents).

Imports and exercises the REAL class (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import (
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
    CONF_SWITCHBOARD_GRID_METER_SENSOR,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
)


def _fake_subentry(
    subentry_id: str, subentry_type: str, data: dict, title: str | None = None
) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = subentry_type
    s.data = data
    s.title = title
    return s


def _fake_entry(subentries: dict, options: dict) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.subentries = subentries
    entry.options = options
    return entry


def test_native_value_counts_only_power_source_subentries():
    subentries = {
        "ps1": _fake_subentry("ps1", SUBENTRY_TYPE_POWER_SOURCE, {}),
        "ps2": _fake_subentry("ps2", SUBENTRY_TYPE_POWER_SOURCE, {}),
        "load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, {}),
    }
    entry = _fake_entry(subentries, {})
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    assert s.native_value == 2


def test_native_value_zero_when_nothing_configured():
    entry = _fake_entry({}, {})
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    assert s.native_value == 0


def test_extra_state_attributes_groups_by_subentry_type():
    subentries = {
        "ps1": _fake_subentry(
            "ps1",
            SUBENTRY_TYPE_POWER_SOURCE,
            {
                CONF_POWER_SOURCE_NAME: "Inverter 1",
                CONF_POWER_SOURCE_BATTERY_SENSOR: "sensor.inv1_battery",
                CONF_POWER_SOURCE_DC_SENSOR: "sensor.inv1_dc",
            },
        ),
        "pv1": _fake_subentry(
            "pv1",
            SUBENTRY_TYPE_PV_STRING,
            {
                CONF_PV_STRING_ENTITY: "sensor.pv_string_1",
                CONF_PV_STRING_LABEL: "String 1",
                CONF_PV_STRING_POWER_SOURCE: "ps1",
            },
        ),
        "bt1": _fake_subentry(
            "bt1",
            SUBENTRY_TYPE_BATTERY_TOWER,
            {CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.tower1_soc"},
            title="Battery Tower 1",
        ),
        "load1": _fake_subentry("load1", SUBENTRY_TYPE_LOAD, {}),
    }
    options = {CONF_SWITCHBOARD_GRID_METER_SENSOR: "sensor.grid_meter"}
    entry = _fake_entry(subentries, options)
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes

    assert len(attrs["power_sources"]) == 1
    assert attrs["power_sources"][0]["subentry_id"] == "ps1"
    assert attrs["power_sources"][0][CONF_POWER_SOURCE_NAME] == "Inverter 1"

    assert len(attrs["pv_strings"]) == 1
    assert attrs["pv_strings"][0][CONF_PV_STRING_POWER_SOURCE] == "ps1"

    assert len(attrs["battery_towers"]) == 1
    assert (
        attrs["battery_towers"][0][CONF_BATTERY_TOWER_SOC_SENSOR] == "sensor.tower1_soc"
    )
    # 2026-08-23: battery towers have no name-like data field of their own
    # (unlike Power Source/PV String) -- title is their only real identity,
    # a real gap found live migrating this household's own 4 real towers.
    assert attrs["battery_towers"][0]["title"] == "Battery Tower 1"

    # Load subentries must never leak into any of these three groups.
    assert all(t["subentry_id"] != "load1" for t in attrs["power_sources"])
    assert all(t["subentry_id"] != "load1" for t in attrs["pv_strings"])
    assert all(t["subentry_id"] != "load1" for t in attrs["battery_towers"])

    assert (
        attrs["switchboard"][CONF_SWITCHBOARD_GRID_METER_SENSOR] == "sensor.grid_meter"
    )


def test_missing_optional_fields_resolve_to_none_not_a_crash():
    # A real, in-progress wizard submission (or an old subentry from
    # before a field was added) has legitimately missing keys -- every
    # field in this class is optional by design.
    subentries = {"ps1": _fake_subentry("ps1", SUBENTRY_TYPE_POWER_SOURCE, {})}
    entry = _fake_entry(subentries, {})
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["power_sources"][0][CONF_POWER_SOURCE_NAME] is None
    assert attrs["switchboard"][CONF_SWITCHBOARD_GRID_METER_SENSOR] is None


def test_battery_tower_title_is_none_not_a_crash_when_unset():
    # A subentry created via async_create_entry() with no explicit title
    # kwarg (shouldn't happen in practice -- _derive_title always returns
    # a real string or the generic fallback -- but the bridge sensor must
    # not assume it's populated) resolves to None cleanly, same discipline
    # as every other optional field in this class.
    subentries = {
        "bt1": _fake_subentry("bt1", SUBENTRY_TYPE_BATTERY_TOWER, {}, title=None),
    }
    entry = _fake_entry(subentries, {})
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    attrs = s.extra_state_attributes
    assert attrs["battery_towers"][0]["title"] is None


def test_entity_id_and_unique_id_are_fixed_one_per_hub():
    entry = _fake_entry({}, {})
    s = sensor.NimbusTopologyConfigSensor(entry, "1.0.0")
    assert s.entity_id == "sensor.nimbus_topology_config"
    assert s._attr_unique_id == "test_entry_topology_config"
