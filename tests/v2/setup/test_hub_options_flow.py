"""The hub options flow: its schemas and steps, and a cleared field staying
cleared (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import voluptuous as vol

from custom_components.nimbus_load.const import (
    ATTR_SIGNAL_ROLE,
    ATTR_SUBENTRY_TYPE,
    CONF_HUMIDITY_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_TEMPERATURE_SENSOR,
)
from custom_components.nimbus_load.flows.hub_options import (
    NimbusHubOptionsFlow,
    _energy_dashboard_switchboard_suggestions,
    _solver_sources_schema,
    _type_safe_entity_suggestions,
)


def _find_marker(schema: vol.Schema, key: str):
    return next(k for k in schema.schema if k == key)


def _make_flow(options: dict) -> NimbusHubOptionsFlow:
    flow = NimbusHubOptionsFlow.__new__(NimbusHubOptionsFlow)
    flow.config_entry = MagicMock(options=options)
    flow._solver_data = {}
    flow.hass = MagicMock()
    return flow


# -- _forecaster_schema: entity fields must use suggested_value, not default --


# -- solver wizard schemas: required vs optional-with-suggested-value -------


# -- "Group A" (2026-08-24): _solver_sources_schema's two load-forecast
# fields get their own candidate lists, when given one --------------------


# -- the household-load field fills itself (1.0.0 one-click install, #1930) --


def _load_default(defaults, candidates):
    schema = _solver_sources_schema(
        defaults, single_load_forecast_candidates=candidates
    )
    return _find_marker(schema, CONF_SOLVER_LOAD_FORECAST_SENSOR).default()


# -- async_step_forecaster: the real merge-not-replace, clear-stays-cleared logic --


# -- the 3-step solver wizard: accumulate-then-chain, only the last step saves --


def _full_solver_data() -> dict:
    return {
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.imp",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.exp",
    }


def test_solver_sources_step_attempts_to_dismiss_the_setup_notification():
    # test_solver_sources_step_attempts_to_dismiss_the_setup_notification
    import asyncio

    flow = _make_flow(options={})
    flow._solver_data = _full_solver_data()
    asyncio.run(
        flow.async_step_solver_sources(
            {
                CONF_SOLVER_SOLAR_FORECAST_SENSOR: "s",
                CONF_SOLVER_LOAD_FORECAST_SENSOR: "s",
            }
        )
    )
    flow.hass.services.async_call.assert_called_once_with(
        "persistent_notification",
        "dismiss",
        {"notification_id": "nimbus_setup_incomplete"},
    )

    # nimbus issue #554: entity_energy_price on a grid source's own flow_from/
    # flow_to entries -- a live price ENTITY the household configured against
    # that flow (HA's schema also allows a fixed number instead, which has
    # nothing to suggest, see test below). Safe to disambiguate import vs
    # export the same way the energy-sensor suggestions above already are:
    # the Energy Dashboard's own flow_from/flow_to structure does it, not a
    # guess by elimination among every monetary sensor -- a genuinely
    # different, safer case than _forecaster_settings_suggestions()'s own
    # deliberate price-sensor exclusion elsewhere in this file.
    # test_suggests_import_and_export_price_sensors_from_grid_flows
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict(
        [
            _energy_state("sensor.real_import", "energy", "total_increasing"),
            _energy_state("sensor.real_export", "energy", "total_increasing"),
            _energy_state("sensor.import_price", "monetary", "measurement"),
            _energy_state("sensor.export_price", "monetary", "measurement"),
        ]
    )
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [
                        {
                            "stat_energy_from": "sensor.real_import",
                            "entity_energy_price": "sensor.import_price",
                        }
                    ],
                    "flow_to": [
                        {
                            "stat_energy_to": "sensor.real_export",
                            "entity_energy_price": "sensor.export_price",
                        }
                    ],
                }
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result[CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR] == "sensor.import_price"
    assert result[CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR] == "sensor.export_price"

    # test_a_fixed_number_price_has_nothing_to_suggest_not_a_crash
    # HA's own schema allows entity_energy_price OR a fixed number
    # (number_energy_price) -- a fixed number is a real, valid household
    # config with no entity to suggest, not a malformed one.
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict([_energy_state("sensor.real_import", "energy", "total_increasing")])
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [
                        {
                            "stat_energy_from": "sensor.real_import",
                            "number_energy_price": 0.28,
                        }
                    ],
                    "flow_to": [],
                }
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result[CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR] == "sensor.real_import"
    assert CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR not in result

    # test_wrong_device_class_is_never_suggested
    # Real, documented precedent this guards against: a plausible-looking
    # sensor that is genuinely the wrong KIND (e.g. a power sensor, or a
    # another optimiser's plan/forecast sensor) must never be proposed at all.
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict(
        [_energy_state("sensor.actually_a_power_sensor", "power", "measurement")]
    )
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.actually_a_power_sensor"}
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR not in result

    # test_type_safe_suggestions_power_and_price_fields_are_never_suggested
    # Deliberately excluded from this function entirely (see its own
    # docstring) -- even a single power/monetary sensor is genuinely
    # ambiguous about WHICH of battery/grid/solar (or import/export) it
    # is, unlike temperature/humidity/SoC which have exactly one real
    # field each.
    hass = _hass_with_all_states(
        [
            _typed_state("sensor.the_only_power_sensor", "power"),
            _typed_state("sensor.the_only_monetary_sensor", "monetary"),
        ]
    )
    assert _type_safe_entity_suggestions(hass) == {}

    # test_forecaster_step_fills_temperature_and_humidity_from_suggestions
    import asyncio

    flow = _make_flow(options={})  # nothing saved yet
    flow.hass.states.async_all = lambda domain: [
        _typed_state("sensor.outdoor_temp", "temperature"),
        _typed_state("sensor.outdoor_humidity", "humidity"),
    ]
    result = asyncio.run(flow.async_step_forecaster(None))
    temp_marker = _find_marker(result["data_schema"], CONF_TEMPERATURE_SENSOR)
    assert temp_marker.description == {"suggested_value": "sensor.outdoor_temp"}
    humidity_marker = _find_marker(result["data_schema"], CONF_HUMIDITY_SENSOR)
    assert humidity_marker.description == {"suggested_value": "sensor.outdoor_humidity"}

    # test_init_step_shows_the_forecaster_vs_solver_vs_switchboard_menu
    import asyncio
    from unittest.mock import AsyncMock, patch

    flow = _make_flow(options={})
    # nimbus #1686: "Review pricing suggestions" is offered only when there
    # is something to review.
    with patch.object(type(flow), "_pricing_suggestions", AsyncMock(return_value=[])):
        result = asyncio.run(flow.async_step_init(None))
    assert result["type"] == "menu"
    assert result["menu_options"] == ["forecaster", "solver_battery", "switchboard"]
    with patch.object(
        type(flow),
        "_pricing_suggestions",
        AsyncMock(return_value=[{"field": "x", "proposed": "sensor.y"}]),
    ):
        result = asyncio.run(flow.async_step_init(None))
    assert result["menu_options"] == [
        "forecaster",
        "solver_battery",
        "switchboard",
        "pricing_suggestions",
    ]

    # test_solver_sources_schema_keeps_saved_multi_values_selectable
    schema = _solver_sources_schema(
        {
            CONF_SOLVER_LOAD_FORECAST_ENTITIES: [
                "sensor.circuit_a",
                "sensor.circuit_gone",
            ]
        },
        single_load_forecast_candidates=["sensor.whole_house_signal"],
        summable_load_forecast_candidates=["sensor.circuit_a", "sensor.circuit_b"],
    )
    multi_marker = _find_marker(schema, CONF_SOLVER_LOAD_FORECAST_ENTITIES)
    assert schema.schema[multi_marker].config["include_entities"] == [
        "sensor.circuit_a",
        "sensor.circuit_b",
        "sensor.circuit_gone",
    ]


# -- async_step_switchboard: same merge-not-replace, clear-stays-cleared
# discipline as async_step_forecaster, applied to the new topology-
# diagram fields (2026-08-23) --


# -- _energy_dashboard_switchboard_suggestions (2026-08-23) --


def _energy_state(entity_id: str, device_class: str, state_class: str):
    st = MagicMock()
    st.attributes = {"device_class": device_class, "state_class": state_class}
    return entity_id, st


def _hass_with_states(states: dict):
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


# -- _discover_nimbus_load_forecast_candidates (2026-08-24, "Group A") ----


def _nimbus_state(entity_id: str, subentry_type: str, signal_role: str | None = None):
    st = MagicMock()
    st.entity_id = entity_id
    attrs = {ATTR_SUBENTRY_TYPE: subentry_type}
    if signal_role is not None:
        attrs[ATTR_SIGNAL_ROLE] = signal_role
    st.attributes = attrs
    return st


def _hass_with_all_states(states: list):
    hass = MagicMock()
    hass.states.async_all = lambda domain: states
    return hass


# -- _type_safe_entity_suggestions (2026-08-24, "Group B") ----------------


def _typed_state(entity_id: str, device_class: str):
    st = MagicMock()
    st.entity_id = entity_id
    st.attributes = {"device_class": device_class}
    return st


# -- nimbus issue #340: include_entities IS enforced at validation, so a
# saved value outside the discovered candidate list must be folded back
# in or the whole Solver wizard becomes unsubmittable -------------------------
