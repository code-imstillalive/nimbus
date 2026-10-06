"""nimbus issue #1562: an energy counter (Wh/kWh) configured where a power
sensor (kW) is expected.

Seen on a tester install, 2026-10-06: the hub's solar sensor was a Wh energy
total. PowerConverter cannot convert Wh, so the coordinator logged
"unconvertible unit 'Wh' -- treating as kW as-is" 52 times in an hour and used
the counter's value as kW. These tests pin the three layers that replace that:
the forms refuse it, startup names it, and the forecaster ignores it as a
feature instead of reading it as power.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load import power_input_check as pic
from custom_components.nimbus_load.const import (
    CONF_BATTERY_SENSOR,
    CONF_GRID_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_SOLAR_SENSOR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.flows import hub_options as ho
from custom_components.nimbus_load.flows.load_subentry import (
    NimbusLoadSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.signal_subentry import (
    NimbusSignalSubentryFlowHandler,
)

UNITS = {
    "sensor.solar_production_total": "Wh",
    "sensor.solar_power": "kW",
    "sensor.grid_power": "W",
    "sensor.battery_energy": "kWh",
    "sensor.no_unit": None,
}


def _hass(units: dict[str, str | None] = UNITS) -> MagicMock:
    hass = MagicMock()

    def _get(entity_id):
        if entity_id not in units:
            return None
        attrs = (
            {}
            if units[entity_id] is None
            else {"unit_of_measurement": units[entity_id]}
        )
        return SimpleNamespace(entity_id=entity_id, state="1.0", attributes=attrs)

    hass.states.get.side_effect = _get
    hass.services.async_call = AsyncMock()
    return hass


# --- unit recognition --------------------------------------------------------


def test_energy_units_are_recognised_case_insensitively():
    for unit in ("Wh", "kWh", "MWh", "wh", "KWH", " kWh ", "GJ", "MJ"):
        assert pic.is_energy_unit(unit), unit


def test_power_and_other_units_are_not_energy():
    for unit in ("W", "kW", "MW", "%", "°C", "", None, 5, MagicMock()):
        assert not pic.is_energy_unit(unit), unit


def test_energy_unit_of_reads_the_live_state():
    hass = _hass()
    assert pic.energy_unit_of(hass, "sensor.solar_production_total") == "Wh"
    assert pic.energy_unit_of(hass, "sensor.solar_power") is None
    assert pic.energy_unit_of(hass, "sensor.no_unit") is None
    # Not loaded yet: not known to be wrong.
    assert pic.energy_unit_of(hass, "sensor.missing") is None
    assert pic.energy_unit_of(hass, None) is None


# --- forms -------------------------------------------------------------------


def test_field_errors_name_only_the_energy_fields():
    hass = _hass()
    errors = pic.energy_unit_field_errors(
        hass,
        {
            CONF_SOLAR_SENSOR: "sensor.solar_production_total",
            CONF_GRID_SENSOR: "sensor.grid_power",
        },
        [CONF_BATTERY_SENSOR, CONF_GRID_SENSOR, CONF_SOLAR_SENSOR],
    )
    assert errors == {CONF_SOLAR_SENSOR: "energy_sensor_not_power"}


def _forecaster_flow(hass, options):
    flow = ho.NimbusHubOptionsFlow.__new__(ho.NimbusHubOptionsFlow)
    flow.hass = hass
    entry = SimpleNamespace(options=dict(options))
    # config_entry is read-only on the real class; the stub accepts an
    # instance attribute, so set it the same way other flow tests set hass.
    try:
        flow.config_entry = entry
    except AttributeError:
        flow._config_entry = entry
    return flow


def test_forecaster_step_refuses_an_energy_counter_and_saves_nothing():
    flow = _forecaster_flow(_hass(), {CONF_SOLAR_SENSOR: "sensor.solar_power"})
    flow.async_create_entry = MagicMock(side_effect=AssertionError("must not save"))
    result = asyncio.run(
        flow.async_step_forecaster({CONF_SOLAR_SENSOR: "sensor.solar_production_total"})
    )
    assert result["type"] == "form"
    assert result["step_id"] == "forecaster"
    assert result["errors"] == {CONF_SOLAR_SENSOR: "energy_sensor_not_power"}


def test_forecaster_step_saves_a_power_sensor():
    flow = _forecaster_flow(_hass(), {})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    result = asyncio.run(
        flow.async_step_forecaster(
            {
                CONF_SOLAR_SENSOR: "sensor.solar_power",
                CONF_GRID_SENSOR: "sensor.grid_power",
            }
        )
    )
    assert result == {"type": "create_entry"}
    saved = flow.async_create_entry.call_args.kwargs["data"]
    assert saved[CONF_SOLAR_SENSOR] == "sensor.solar_power"


def _subentry_flow(cls, hass):
    flow = cls.__new__(cls)
    flow.source = "user"
    flow.hass = hass
    flow._get_entry = MagicMock(return_value=SimpleNamespace(subentries={}))
    return flow


def test_load_and_signal_flows_refuse_an_energy_counter():
    for cls in (NimbusLoadSubentryFlowHandler, NimbusSignalSubentryFlowHandler):
        flow = _subentry_flow(cls, _hass())
        flow.async_create_entry = MagicMock(side_effect=AssertionError("must not save"))
        result = asyncio.run(
            flow.async_step_user({CONF_LOAD_SENSOR: "sensor.battery_energy"})
        )
        assert result["type"] == "form", cls.__name__
        assert result["errors"] == {CONF_LOAD_SENSOR: "energy_sensor_not_power"}, (
            cls.__name__
        )


def test_load_flow_accepts_a_power_sensor():
    flow = _subentry_flow(NimbusLoadSubentryFlowHandler, _hass())
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    result = asyncio.run(flow.async_step_user({CONF_LOAD_SENSOR: "sensor.grid_power"}))
    assert result == {"type": "create_entry"}


# --- runtime: the feature is ignored, not read as kW --------------------------


def _coordinator(hass, options, load_sensor="sensor.some_load"):
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = hass
    coord.entry = SimpleNamespace(options=options)
    coord.subentry = SimpleNamespace(
        subentry_id="s1",
        subentry_type=SUBENTRY_TYPE_LOAD,
        data={CONF_LOAD_SENSOR: load_sensor},
    )
    return coord


def test_an_energy_counter_feature_is_treated_as_not_configured(caplog):
    pic._WARNED.clear()
    coord = _coordinator(
        _hass(),
        {
            CONF_SOLAR_SENSOR: "sensor.solar_production_total",
            CONF_GRID_SENSOR: "sensor.grid_power",
            CONF_BATTERY_SENSOR: "sensor.battery_energy",
        },
    )
    with caplog.at_level(logging.WARNING):
        assert coord._solar_sensor is None
        assert coord._battery_sensor is None
        assert coord._grid_sensor == "sensor.grid_power"
        # asked every cycle; warned once per entity
        assert coord._solar_sensor is None
    warnings = [
        r.getMessage() for r in caplog.records if "energy unit" in r.getMessage()
    ]
    assert len(warnings) == 2, warnings
    assert any("sensor.solar_production_total" in w and "'Wh'" in w for w in warnings)


def test_the_existing_self_reference_rule_still_holds():
    coord = _coordinator(
        _hass(),
        {CONF_GRID_SENSOR: "sensor.grid_power"},
        load_sensor="sensor.grid_power",
    )
    assert coord._grid_sensor is None


def test_a_sensor_not_loaded_yet_is_not_dropped():
    coord = _coordinator(_hass(), {CONF_SOLAR_SENSOR: "sensor.missing"})
    assert coord._solar_sensor == "sensor.missing"


def test_the_conversion_warning_names_an_energy_unit_for_what_it_is():
    energy = coordinator_module._unconvertible_unit_message("Wh") % ("sensor.x", "Wh")
    assert "energy counter" in energy and "treating as kW" not in energy
    lts = coordinator_module._unconvertible_unit_message("kWh", lts=True) % (
        "sensor.x",
        "kWh",
    )
    assert lts.startswith("sensor.x LTS reports") and "energy counter" in lts
    other = coordinator_module._unconvertible_unit_message("VA") % ("sensor.x", "VA")
    assert other == "sensor.x reported unconvertible unit 'VA' -- treating as kW as-is"


# --- startup notification ----------------------------------------------------


def _entry(options, *subentries):
    return SimpleNamespace(
        options=options, subentries={s.subentry_id: s for s in subentries}
    )


def _sub(sid, kind, sensor, title):
    return SimpleNamespace(
        subentry_id=sid,
        subentry_type=kind,
        data={CONF_LOAD_SENSOR: sensor},
        title=title,
    )


def test_find_names_hub_inputs_and_load_or_signal_sensors_only():
    entry = _entry(
        {
            CONF_SOLAR_SENSOR: "sensor.solar_production_total",
            CONF_GRID_SENSOR: "sensor.grid_power",
        },
        _sub("a", SUBENTRY_TYPE_LOAD, "sensor.battery_energy", "Heater"),
        _sub("b", SUBENTRY_TYPE_SIGNAL, "sensor.solar_power", "Solar"),
        _sub("c", SUBENTRY_TYPE_POWER_SOURCE, "sensor.battery_energy", "PS"),
    )
    found = pic.find_energy_unit_inputs(_hass(), entry)
    assert found == [
        ("Forecaster: solar sensor", "sensor.solar_production_total", "Wh"),
        ("Load 'Heater'", "sensor.battery_energy", "kWh"),
    ]


def test_notification_is_created_when_found_and_dismissed_when_clean():
    hass = _hass()
    bad = _entry({CONF_SOLAR_SENSOR: "sensor.solar_production_total"})
    asyncio.run(pic.async_notify_energy_unit_inputs(hass, bad))
    domain, service, data = hass.services.async_call.call_args.args[:3]
    assert (domain, service) == ("persistent_notification", "create")
    assert data["notification_id"] == pic.NOTIFICATION_ID
    assert (
        "sensor.solar_production_total" in data["message"] and "Wh" in data["message"]
    )

    hass.services.async_call.reset_mock()
    asyncio.run(
        pic.async_notify_energy_unit_inputs(
            hass, _entry({CONF_SOLAR_SENSOR: "sensor.solar_power"})
        )
    )
    domain, service, data = hass.services.async_call.call_args.args[:3]
    assert (domain, service, data) == (
        "persistent_notification",
        "dismiss",
        {"notification_id": pic.NOTIFICATION_ID},
    )


def test_the_notifier_never_raises():
    hass = _hass()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(
        pic.async_notify_energy_unit_inputs(
            hass, _entry({CONF_SOLAR_SENSOR: "sensor.solar_production_total"})
        )
    )
