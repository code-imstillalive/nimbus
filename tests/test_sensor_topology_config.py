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
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import (
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
    CONF_BATTERY_PARTICIPANT_NAME,
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
    CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
    CONF_SWITCHBOARD_GRID_METER_SENSOR,
    SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
)


def _sensor_with_hass(
    entry: MagicMock, capacity_state: str | None = "40.3"
) -> sensor.NimbusTopologyConfigSensor:
    """A NimbusTopologyConfigSensor with .hass wired up for
    _resolve_live_number()'s registry + states.get() lookups -- same
    construction technique test_sensor_solver_config_entity_registry_
    resolution.py already uses for the sibling bridge sensor."""
    instance = sensor.NimbusTopologyConfigSensor.__new__(
        sensor.NimbusTopologyConfigSensor
    )
    sensor.NimbusTopologyConfigSensor.__init__(instance, entry, "1.0.0")
    instance.hass = MagicMock()
    if capacity_state is None:
        instance.hass.states.get = MagicMock(return_value=None)
    else:
        instance.hass.states.get = MagicMock(
            return_value=MagicMock(state=capacity_state)
        )
    return instance


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


# ─── nimbus issue #575: derived topology from the Solver's own config ──────


def test_derives_a_home_power_source_pv_string_and_battery_tower():
    options = {
        CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.sigen_plant_battery_power",
        CONF_SOLVER_SOLAR_POWER_SENSOR: "sensor.sigen_plant_pv_power",
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.sigen_plant_battery_state_of_charge",
    }
    entry = _fake_entry({}, options)
    s = _sensor_with_hass(entry, capacity_state="40.3")

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        attrs = s.extra_state_attributes
        native_value = s.native_value

    assert native_value == 1

    assert len(attrs["power_sources"]) == 1
    ps = attrs["power_sources"][0]
    assert ps["derived"] is True
    assert ps[CONF_POWER_SOURCE_BATTERY_SENSOR] == "sensor.sigen_plant_battery_power"
    assert ps[CONF_POWER_SOURCE_DC_SENSOR] == "sensor.sigen_plant_pv_power"

    assert len(attrs["pv_strings"]) == 1
    pv = attrs["pv_strings"][0]
    assert pv["derived"] is True
    assert pv[CONF_PV_STRING_ENTITY] == "sensor.sigen_plant_pv_power"
    assert pv[CONF_PV_STRING_POWER_SOURCE] == ps["subentry_id"]

    assert len(attrs["battery_towers"]) == 1
    tower = attrs["battery_towers"][0]
    assert tower["derived"] is True
    assert (
        tower[CONF_BATTERY_TOWER_SOC_SENSOR]
        == "sensor.sigen_plant_battery_state_of_charge"
    )
    assert tower[CONF_BATTERY_TOWER_POWER_SOURCE] == ps["subentry_id"]
    assert tower["capacity_kwh"] == 40.3

    # Confirms _resolve_live_number() went through the same registry-
    # lookup-then-literal-fallback path as NimbusSolverConfigSensor's own
    # _resolve() (nimbus issue #343), not a guessed literal directly.
    s.hass.states.get.assert_any_call(
        f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}"
    )


def test_derives_one_power_source_and_tower_per_battery_participant():
    options = {
        CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.sigen_plant_battery_power",
        CONF_SOLVER_SOLAR_POWER_SENSOR: "sensor.sigen_plant_pv_power",
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.sigen_plant_battery_state_of_charge",
    }
    subentries = {
        "ev_m3p": _fake_subentry(
            "ev_m3p",
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            {
                CONF_BATTERY_PARTICIPANT_NAME: "Model 3 Performance",
                CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.m3p_t_battery_level",
                CONF_BATTERY_PARTICIPANT_POWER_SENSOR: (
                    "sensor.sigen_inverter_dc_charger_output_power"
                ),
                CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 82.0,
                CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP: "sigen_dc_charger",
            },
        ),
        "ev_my": _fake_subentry(
            "ev_my",
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            {
                CONF_BATTERY_PARTICIPANT_NAME: "Model Y",
                CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.my_t_battery_level",
                CONF_BATTERY_PARTICIPANT_POWER_SENSOR: (
                    "sensor.sigen_inverter_dc_charger_output_power"
                ),
                CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 75.0,
                CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP: "sigen_dc_charger",
            },
        ),
    }
    entry = _fake_entry(subentries, options)
    s = _sensor_with_hass(entry, capacity_state="40.3")

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        attrs = s.extra_state_attributes
        native_value = s.native_value

    # Home source + 2 participant sources = 3.
    assert native_value == 3
    assert len(attrs["power_sources"]) == 3
    assert len(attrs["battery_towers"]) == 3

    participant_sources = [
        p for p in attrs["power_sources"] if p["subentry_id"] != "derived_home"
    ]
    assert len(participant_sources) == 2
    # Shared-charger group name wins over the participant's own name.
    assert all(
        p[CONF_POWER_SOURCE_NAME] == "sigen_dc_charger" for p in participant_sources
    )
    assert all(p["derived"] is True for p in participant_sources)

    participant_towers = [t for t in attrs["battery_towers"] if t["title"] != "Nimbus"]
    titles = {t["title"] for t in participant_towers}
    assert titles == {"Model 3 Performance", "Model Y"}
    socs = {t[CONF_BATTERY_TOWER_SOC_SENSOR] for t in participant_towers}
    assert socs == {"sensor.m3p_t_battery_level", "sensor.my_t_battery_level"}
    capacities = {t["capacity_kwh"] for t in participant_towers}
    assert capacities == {82.0, 75.0}


def test_a_real_power_source_subentry_disables_derivation_entirely():
    options = {
        CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.sigen_plant_battery_power",
        CONF_SOLVER_SOLAR_POWER_SENSOR: "sensor.sigen_plant_pv_power",
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.sigen_plant_battery_state_of_charge",
    }
    subentries = {
        "ps1": _fake_subentry(
            "ps1",
            SUBENTRY_TYPE_POWER_SOURCE,
            {CONF_POWER_SOURCE_NAME: "Real Inverter"},
        ),
    }
    entry = _fake_entry(subentries, options)
    s = _sensor_with_hass(entry)
    attrs = s.extra_state_attributes

    assert len(attrs["power_sources"]) == 1
    assert attrs["power_sources"][0][CONF_POWER_SOURCE_NAME] == "Real Inverter"
    assert "derived" not in attrs["power_sources"][0]
    assert attrs["pv_strings"] == []
    assert attrs["battery_towers"] == []
    assert s.native_value == 1


def test_unconfigured_solver_derives_nothing_not_a_crash():
    entry = _fake_entry({}, {})
    s = _sensor_with_hass(entry)

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        attrs = s.extra_state_attributes
        native_value = s.native_value

    assert attrs["power_sources"] == []
    assert attrs["pv_strings"] == []
    assert attrs["battery_towers"] == []
    assert native_value == 0


def test_battery_participant_with_no_soc_sensor_still_derives_cleanly():
    # A real, in-progress participant wizard submission has legitimately
    # missing optional fields -- same discipline as the rest of this
    # class's optional-field handling.
    options = {CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.sigen_plant_battery_power"}
    subentries = {
        "ev1": _fake_subentry(
            "ev1",
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            {CONF_BATTERY_PARTICIPANT_NAME: "New EV"},
        ),
    }
    entry = _fake_entry(subentries, options)
    s = _sensor_with_hass(entry, capacity_state=None)

    with patch.object(sensor.er, "async_get") as mock_async_get:
        mock_async_get.return_value.async_get_entity_id.return_value = None
        attrs = s.extra_state_attributes

    participant_towers = [t for t in attrs["battery_towers"] if t["title"] == "New EV"]
    assert len(participant_towers) == 1
    assert participant_towers[0][CONF_BATTERY_TOWER_SOC_SENSOR] is None
    assert participant_towers[0]["capacity_kwh"] is None
