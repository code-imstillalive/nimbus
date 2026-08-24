"""Real test of flows/hub_options.py -- both its schema-building functions
and NimbusHubOptionsFlow's own step logic, including the real, documented
2026-08-22 merge-logic bug this exists to guard against ("its not letting
me delete anything it remains there even after deleting").

Imports and exercises the REAL functions/methods (not a reimplementation)
against real `voluptuous` (a genuine, small, standalone PyPI package --
installed locally specifically so schema assertions test real behavior,
not a faked-up stand-in for a validation library) and tests/_ha_stubs.py's
stand-in homeassistant.* modules for everything HA-specific.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voluptuous as vol

from custom_components.nimbus_load.const import (
    ATTR_SIGNAL_ROLE,
    ATTR_SUBENTRY_TYPE,
    CONF_BATTERY_SENSOR,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_GRID,
    SIGNAL_ROLE_OTHER,
    SIGNAL_ROLE_SOLAR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.flows.hub_options import (
    NimbusHubOptionsFlow,
    _discover_nimbus_load_forecast_candidates,
    _energy_dashboard_switchboard_suggestions,
    _forecaster_schema,
    _solver_battery_schema,
    _solver_grid_schema,
    _solver_sources_schema,
    _switchboard_schema,
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


def test_forecaster_schema_entity_field_uses_suggested_value_not_default():
    # The exact 2026-08-22 fix this guards against: a plain default= on an
    # entity field makes voluptuous silently re-inject the OLD value the
    # instant a user clears the picker and submits, since the frontend
    # omits the key entirely for a cleared field.
    schema = _forecaster_schema({CONF_TEMPERATURE_SENSOR: "sensor.outdoor_temp"})
    marker = _find_marker(schema, CONF_TEMPERATURE_SENSOR)
    assert marker.default is vol.UNDEFINED
    assert marker.description == {"suggested_value": "sensor.outdoor_temp"}


def test_forecaster_schema_entity_field_with_no_stored_value_yet():
    schema = _forecaster_schema({})
    marker = _find_marker(schema, CONF_TEMPERATURE_SENSOR)
    assert marker.default is vol.UNDEFINED
    assert marker.description == {"suggested_value": None}


def test_forecaster_schema_numeric_field_uses_real_default_not_suggested_value():
    # Numeric fields (horizon/retrain hour/train days) are fine with a
    # real default= -- HA won't let a Required-style numeric field submit
    # truly empty anyway, so "sticky" is correct behaviour there (per the
    # function's own docstring).
    schema = _forecaster_schema({CONF_FORECAST_HORIZON_HOURS: 72})
    marker = _find_marker(schema, CONF_FORECAST_HORIZON_HOURS)
    assert marker.default is not vol.UNDEFINED
    assert marker.default() == 72
    assert marker.description is None


def test_forecaster_schema_numeric_field_falls_back_to_its_own_module_default():
    schema = _forecaster_schema({})  # nothing stored yet -- a fresh install
    marker = _find_marker(schema, CONF_FORECAST_HORIZON_HOURS)
    # Whatever DEFAULT_FORECAST_HORIZON_HOURS resolves to -- just confirm
    # it's a real, non-None number, not that a stale/absent value crashes.
    assert isinstance(marker.default(), (int, float))


def test_forecaster_schema_battery_field_is_switch_domain_not_sensor():
    # Curtailment is deliberately domain="switch" -- a genuinely different
    # entity type than every other field on this form.
    from custom_components.nimbus_load.const import CONF_CURTAILMENT_SENSOR

    schema = _forecaster_schema({})
    marker = _find_marker(schema, CONF_CURTAILMENT_SENSOR)
    selector_instance = schema.schema[marker]
    assert selector_instance.config["domain"] == "switch"
    battery_marker = _find_marker(schema, CONF_BATTERY_SENSOR)
    battery_selector = schema.schema[battery_marker]
    assert battery_selector.config["domain"] == "sensor"


def test_forecaster_schema_temperature_forecast_field_accepts_weather_domain():
    # #123 (Mark Purcell, real repro, 2026-08-24): modern HA weather
    # entities no longer expose a "forecast" state attribute at all --
    # domain="sensor" alone locked out the natural, simplest choice
    # (point straight at weather.home). Must now accept BOTH weather.*
    # (fetched internally via weather.get_forecasts) and sensor.* (an
    # already-working template sensor, fully backward compatible).
    schema = _forecaster_schema({})
    marker = _find_marker(schema, CONF_TEMPERATURE_FORECAST_SENSOR)
    selector_instance = schema.schema[marker]
    domain = selector_instance.config["domain"]
    assert set(domain) == {"sensor", "weather"}


# -- solver wizard schemas: required vs optional-with-suggested-value -------


def test_solver_sources_schema_second_source_is_optional_with_suggested_value():
    schema = _solver_sources_schema(
        {CONF_SOLVER_SOLAR_FORECAST_SENSOR_2: "sensor.openmeteo"}
    )
    marker = _find_marker(schema, CONF_SOLVER_SOLAR_FORECAST_SENSOR_2)
    assert type(marker).__name__ == "Optional"
    assert marker.default is vol.UNDEFINED
    assert marker.description == {"suggested_value": "sensor.openmeteo"}


def test_solver_sources_schema_primary_source_is_required():
    schema = _solver_sources_schema(
        {CONF_SOLVER_SOLAR_FORECAST_SENSOR: "sensor.solcast"}
    )
    marker = _find_marker(schema, CONF_SOLVER_SOLAR_FORECAST_SENSOR)
    assert type(marker).__name__ == "Required"
    assert marker.default() == "sensor.solcast"


def test_solver_battery_schema_has_one_required_soc_field():
    schema = _solver_battery_schema({})
    # 2026-08-24, nimbus #125: gained a second, genuinely OPTIONAL field
    # (solver_max_discharge_live_entity) -- the SoC sensor stays the only
    # Required one.
    assert len(schema.schema) == 2
    marker = _find_marker(schema, CONF_SOLVER_BATTERY_SOC_SENSOR)
    assert type(marker).__name__ == "Required"


def test_solver_battery_schema_max_discharge_live_entity_is_optional_number_domain():
    # #125 (Mark Purcell, real repro): a bare hardcoded entity_id used to
    # silently override a portable install's configured
    # solver_max_discharge_kw whenever an unrelated entity happened to
    # exist at that exact name. Now a genuine, optional, per-household
    # field -- Optional (not Required, unlike the SoC sensor above) so
    # it's a complete no-op when left unset, matching every other
    # optional entity-pointer field's own convention (see
    # _forecaster_schema's own 2026-08-22/2026-08-24 comments for why
    # this must be description={"suggested_value": ...}, not default=).
    schema = _solver_battery_schema({})
    marker = _find_marker(schema, CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY)
    assert type(marker).__name__ == "Optional"
    selector_instance = schema.schema[marker]
    assert selector_instance.config["domain"] == "number"


def test_solver_grid_schema_has_two_required_price_fields():
    schema = _solver_grid_schema({})
    assert len(schema.schema) == 2
    for key in (CONF_SOLVER_IMPORT_PRICE_SENSOR, CONF_SOLVER_EXPORT_PRICE_SENSOR):
        assert type(_find_marker(schema, key)).__name__ == "Required"


# -- "Group A" (2026-08-24): _solver_sources_schema's two load-forecast
# fields get their own candidate lists, when given one --------------------


def test_solver_sources_schema_with_no_candidates_is_fully_unrestricted():
    # The exact original behaviour, byte-identical -- both new params
    # default to None, and _entity()/_entity_multi() only add
    # include_entities to the selector config when given a genuinely
    # non-empty list. No caller anywhere (real or test) should ever see
    # this field artificially narrowed unless discovery actually ran.
    schema = _solver_sources_schema({})
    for key in (CONF_SOLVER_LOAD_FORECAST_SENSOR, CONF_SOLVER_LOAD_FORECAST_ENTITIES):
        marker = _find_marker(schema, key)
        selector_instance = schema.schema[marker]
        assert "include_entities" not in selector_instance.config


def test_solver_sources_schema_single_candidates_restrict_only_that_field():
    schema = _solver_sources_schema(
        {},
        single_load_forecast_candidates=["sensor.whole_house_signal"],
        summable_load_forecast_candidates=["sensor.circuit_a", "sensor.circuit_b"],
    )
    single_marker = _find_marker(schema, CONF_SOLVER_LOAD_FORECAST_SENSOR)
    assert schema.schema[single_marker].config["include_entities"] == [
        "sensor.whole_house_signal"
    ]
    multi_marker = _find_marker(schema, CONF_SOLVER_LOAD_FORECAST_ENTITIES)
    assert schema.schema[multi_marker].config["include_entities"] == [
        "sensor.circuit_a",
        "sensor.circuit_b",
    ]
    # Every OTHER field on this same schema step is untouched -- Group A
    # only ever narrows the two load-forecast fields, never the solar
    # sources or the whole-house cross-check sensor.
    solar_marker = _find_marker(schema, CONF_SOLVER_SOLAR_FORECAST_SENSOR)
    assert "include_entities" not in schema.schema[solar_marker].config


def test_solver_sources_schema_empty_candidate_lists_are_treated_as_no_restriction():
    # A real, live discovery failure (or a genuinely fresh install with
    # zero load/signal subentries configured yet) returns [] -- must
    # degrade to "show everything", never to "show nothing at all".
    schema = _solver_sources_schema(
        {}, single_load_forecast_candidates=[], summable_load_forecast_candidates=[]
    )
    for key in (CONF_SOLVER_LOAD_FORECAST_SENSOR, CONF_SOLVER_LOAD_FORECAST_ENTITIES):
        marker = _find_marker(schema, key)
        assert "include_entities" not in schema.schema[marker].config


# -- async_step_forecaster: the real merge-not-replace, clear-stays-cleared logic --


def test_forecaster_step_no_input_shows_the_form():
    flow = _make_flow(options={CONF_TEMPERATURE_SENSOR: "sensor.old"})
    import asyncio

    result = asyncio.run(flow.async_step_forecaster(None))
    assert result["type"] == "form"
    assert result["step_id"] == "forecaster"


def test_forecaster_step_genuinely_cleared_field_stays_cleared():
    # The real 2026-08-22 bug: a plain {**old, **user_input} spread treats
    # an OMITTED key (the frontend's real submission shape for "user
    # cleared this field") as "untouched", so the old value survives
    # forever. The fix takes user_input.get(key) explicitly for every
    # schema-defined key -- omitted resolves to None correctly.
    import asyncio

    flow = _make_flow(options={CONF_TEMPERATURE_SENSOR: "sensor.old_stale_value"})
    # user_input omits CONF_TEMPERATURE_SENSOR entirely -- exactly what a
    # real cleared-field submission looks like.
    result = asyncio.run(flow.async_step_forecaster({}))
    assert result["type"] == "create_entry"
    assert result["data"][CONF_TEMPERATURE_SENSOR] is None


def test_forecaster_step_untouched_dashboard_value_is_preserved():
    # A number.nimbus_solver_* dashboard value (or any key outside this
    # schema's own field list) must survive this form's submission
    # completely untouched -- the ORIGINAL risk the 2026-08-17 merge fix
    # protected against, still required to hold after the 2026-08-22 fix.
    import asyncio

    flow = _make_flow(options={"solver_battery_capacity_kwh": 122.2})
    result = asyncio.run(
        flow.async_step_forecaster({CONF_TEMPERATURE_SENSOR: "sensor.new"})
    )
    assert result["data"]["solver_battery_capacity_kwh"] == 122.2


def test_forecaster_step_real_submitted_value_is_used():
    import asyncio

    flow = _make_flow(options={CONF_TEMPERATURE_SENSOR: "sensor.old"})
    result = asyncio.run(
        flow.async_step_forecaster({CONF_TEMPERATURE_SENSOR: "sensor.new"})
    )
    assert result["data"][CONF_TEMPERATURE_SENSOR] == "sensor.new"


# -- the 3-step solver wizard: accumulate-then-chain, only the last step saves --


def test_solver_battery_step_with_no_input_shows_its_own_form():
    import asyncio

    flow = _make_flow(options={})
    result = asyncio.run(flow.async_step_solver_battery(None))
    assert result["type"] == "form"
    assert result["step_id"] == "solver_battery"


def test_solver_battery_step_submission_chains_straight_to_grid_form():
    import asyncio

    flow = _make_flow(options={})
    result = asyncio.run(
        flow.async_step_solver_battery({CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc"})
    )
    assert result["type"] == "form"
    assert result["step_id"] == "solver_grid"
    assert flow._solver_data == {CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc"}


def test_solver_grid_step_submission_chains_straight_to_sources_form():
    import asyncio

    flow = _make_flow(options={})
    flow._solver_data = {
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc"
    }  # from the previous step
    result = asyncio.run(
        flow.async_step_solver_grid(
            {
                CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.imp",
                CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.exp",
            }
        )
    )
    assert result["step_id"] == "solver_sources"
    # Accumulated ACROSS steps, not replaced.
    assert flow._solver_data == {
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.imp",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.exp",
    }


def _full_solver_data() -> dict:
    return {
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.imp",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.exp",
    }


def test_solver_sources_step_saves_and_preserves_untouched_keys():
    import asyncio

    flow = _make_flow(
        options={
            "solver_battery_capacity_kwh": 122.2,
            CONF_TEMPERATURE_SENSOR: "sensor.outdoor",
        }
    )
    flow._solver_data = _full_solver_data()
    result = asyncio.run(
        flow.async_step_solver_sources(
            {
                CONF_SOLVER_SOLAR_FORECAST_SENSOR: "sensor.solcast",
                CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.load_fc",
            }
        )
    )
    assert result["type"] == "create_entry"
    # Real, untouched dashboard/other-form values survive.
    assert result["data"]["solver_battery_capacity_kwh"] == 122.2
    assert result["data"][CONF_TEMPERATURE_SENSOR] == "sensor.outdoor"
    # Fields the 3-step wizard actually collected are all present.
    assert result["data"][CONF_SOLVER_BATTERY_SOC_SENSOR] == "sensor.soc"
    assert result["data"][CONF_SOLVER_SOLAR_FORECAST_SENSOR] == "sensor.solcast"
    # A genuinely-optional wizard field never touched this run resolves to
    # None, not silently missing or silently stale.
    assert result["data"][CONF_SOLVER_SOLAR_FORECAST_SENSOR_2] is None


def test_solver_sources_step_attempts_to_dismiss_the_setup_notification():
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


def test_solver_sources_step_save_never_blocked_by_a_failing_notification():
    import asyncio

    flow = _make_flow(options={})
    flow._solver_data = _full_solver_data()
    flow.hass.services.async_call = MagicMock(
        side_effect=RuntimeError("hass not ready")
    )
    result = asyncio.run(
        flow.async_step_solver_sources(
            {
                CONF_SOLVER_SOLAR_FORECAST_SENSOR: "s",
                CONF_SOLVER_LOAD_FORECAST_SENSOR: "s",
            }
        )
    )
    assert result["type"] == "create_entry"  # the real save still succeeded


# -- async_step_switchboard: same merge-not-replace, clear-stays-cleared
# discipline as async_step_forecaster, applied to the new topology-
# diagram fields (2026-08-23) --


def test_switchboard_step_no_input_shows_the_form():
    flow = _make_flow(options={CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.old"})
    import asyncio

    result = asyncio.run(flow.async_step_switchboard(None))
    assert result["type"] == "form"
    assert result["step_id"] == "switchboard"


def test_switchboard_step_genuinely_cleared_field_stays_cleared():
    import asyncio

    flow = _make_flow(
        options={CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.old_stale_value"}
    )
    result = asyncio.run(flow.async_step_switchboard({}))
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR] is None


def test_switchboard_step_untouched_dashboard_value_is_preserved():
    import asyncio

    flow = _make_flow(options={"solver_battery_capacity_kwh": 122.2})
    result = asyncio.run(
        flow.async_step_switchboard(
            {CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.new"}
        )
    )
    assert result["data"]["solver_battery_capacity_kwh"] == 122.2


def test_switchboard_step_real_submitted_value_is_used():
    import asyncio

    flow = _make_flow(options={CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.old"})
    result = asyncio.run(
        flow.async_step_switchboard(
            {CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.new"}
        )
    )
    assert result["data"][CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR] == "sensor.new"


def test_switchboard_schema_every_field_is_optional():
    # Every field must be Optional -- a fresh install with none of this
    # filled in must still get a real diagram (see const.py's own
    # comment above the CONF_SWITCHBOARD_* fields). 8, not 10 (2026-08-
    # 23): grid_meter/battery_power are gone from this form entirely,
    # auto-detected via Power Signal role instead -- see hub_options.py's
    # own comment on _SWITCHBOARD_SCHEMA_KEYS.
    schema = _switchboard_schema({})
    assert len(schema.schema) == 8
    for key in schema.schema:
        assert type(key).__name__ == "Optional"


# -- _energy_dashboard_switchboard_suggestions (2026-08-23) --


def _energy_state(entity_id: str, device_class: str, state_class: str):
    st = MagicMock()
    st.attributes = {"device_class": device_class, "state_class": state_class}
    return entity_id, st


def _hass_with_states(states: dict):
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


def test_suggests_grid_import_and_export_when_type_and_class_match():
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict(
        [
            _energy_state("sensor.real_import", "energy", "total_increasing"),
            _energy_state("sensor.real_export", "energy", "total_increasing"),
        ]
    )
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [{"stat_energy_from": "sensor.real_import"}],
                    "flow_to": [{"stat_energy_to": "sensor.real_export"}],
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
    assert result[CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR] == "sensor.real_export"
    # No Energy Dashboard concept of whole-house load -- must never guess.
    assert CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR not in result


def test_suggests_solar_and_battery_when_type_and_class_match():
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict(
        [
            _energy_state("sensor.real_solar", "energy", "total_increasing"),
            _energy_state("sensor.real_discharge", "energy", "total"),
            _energy_state("sensor.real_charge", "energy", "total"),
        ]
    )
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.real_solar"},
                {
                    "type": "battery",
                    "stat_energy_from": "sensor.real_discharge",
                    "stat_energy_to": "sensor.real_charge",
                },
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result[CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR] == "sensor.real_solar"
    assert (
        result[CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR]
        == "sensor.real_discharge"
    )
    assert result[CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR] == "sensor.real_charge"


def test_wrong_device_class_is_never_suggested():
    # Real, documented precedent this guards against: a plausible-looking
    # sensor that is genuinely the wrong KIND (e.g. a power sensor, or a
    # HAEO plan/forecast sensor) must never be proposed at all.
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


def test_wrong_state_class_is_never_suggested():
    import asyncio
    from unittest.mock import AsyncMock, patch

    states = dict([_energy_state("sensor.instantaneous_only", "energy", "measurement")])
    hass = _hass_with_states(states)
    manager = MagicMock(
        data={
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.instantaneous_only"}
            ]
        }
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR not in result


def test_no_energy_dashboard_configured_degrades_to_empty_not_a_crash():
    import asyncio
    from unittest.mock import AsyncMock, patch

    hass = _hass_with_states({})
    manager = MagicMock(data={})  # nothing configured at all
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result == {}


def test_manager_api_failure_degrades_to_empty_never_raises():
    # The real, honest reason for the broad except in the real function:
    # this is genuinely-internal HA API, not a stable public contract --
    # any failure here (component not loaded, shape changed) must never
    # break the wizard.
    import asyncio
    from unittest.mock import AsyncMock, patch

    hass = _hass_with_states({})
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(side_effect=RuntimeError("energy component not loaded")),
    ):
        result = asyncio.run(_energy_dashboard_switchboard_suggestions(hass))
    assert result == {}


def test_switchboard_step_prefers_saved_value_over_energy_dashboard_suggestion():
    # Safeguard 2 from the real function's own docstring: a real saved
    # value must never be silently overwritten by a fresh suggestion,
    # every single time this form is opened.
    import asyncio
    from unittest.mock import AsyncMock, patch

    flow = _make_flow(
        options={
            CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR: "sensor.household_own_real_choice"
        }
    )
    manager = MagicMock(
        data={
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.energy_dashboard_guess"}
            ]
        }
    )
    flow.hass.states.get = lambda eid: (
        MagicMock(
            attributes={"device_class": "energy", "state_class": "total_increasing"}
        )
        if eid == "sensor.energy_dashboard_guess"
        else None
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(flow.async_step_switchboard(None))
    marker = _find_marker(
        result["data_schema"], CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR
    )
    assert marker.description == {"suggested_value": "sensor.household_own_real_choice"}


def test_switchboard_step_fills_a_genuine_gap_with_a_suggestion():
    import asyncio
    from unittest.mock import AsyncMock, patch

    flow = _make_flow(options={})  # nothing saved yet at all
    manager = MagicMock(
        data={
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.energy_dashboard_guess"}
            ]
        }
    )
    flow.hass.states.get = lambda eid: (
        MagicMock(
            attributes={"device_class": "energy", "state_class": "total_increasing"}
        )
        if eid == "sensor.energy_dashboard_guess"
        else None
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(flow.async_step_switchboard(None))
    marker = _find_marker(
        result["data_schema"], CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR
    )
    assert marker.description == {"suggested_value": "sensor.energy_dashboard_guess"}
    # Still fully optional, still overridable, still never a locked-in default.
    assert marker.default is vol.UNDEFINED


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


def test_discover_no_nimbus_entities_at_all_returns_two_empty_lists():
    hass = _hass_with_all_states([])
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert single == []
    assert summable == []


def test_discover_a_non_nimbus_sensor_is_ignored():
    # A real sensor entity with no ATTR_SUBENTRY_TYPE at all (the vast
    # majority of any live system's own entities) -- and, critically,
    # sensor.nimbus_household_load_total_forecast itself (a real
    # _NimbusSolverPushSensor, confirmed via direct source read to never
    # carry ATTR_SUBENTRY_TYPE) must never appear in either list -- that
    # is exactly the real, confirmed #118 circular-reference footgun
    # this function exists to stop an installer from ever being offered.
    plain = MagicMock()
    plain.entity_id = "sensor.some_unrelated_thing"
    plain.attributes = {}
    aggregate = MagicMock()
    aggregate.entity_id = "sensor.nimbus_household_load_total_forecast"
    aggregate.attributes = {}
    hass = _hass_with_all_states([plain, aggregate])
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert single == []
    assert summable == []


def test_discover_load_subentry_forecasts_go_in_both_lists():
    circuit = _nimbus_state("sensor.nimbus_pool_pump_load_forecast", SUBENTRY_TYPE_LOAD)
    hass = _hass_with_all_states([circuit])
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert single == ["sensor.nimbus_pool_pump_load_forecast"]
    assert summable == ["sensor.nimbus_pool_pump_load_forecast"]


def test_discover_other_role_power_signal_goes_in_single_only():
    # A household's own "Whole House Load"/"CB Total Combined Power"-
    # style Power Signal -- explicit role "other" (never battery/solar/
    # grid), exactly the real, correct single-sensor answer.
    whole_house = _nimbus_state(
        "sensor.nimbus_whole_house_load_signal_forecast",
        SUBENTRY_TYPE_SIGNAL,
        signal_role=SIGNAL_ROLE_OTHER,
    )
    hass = _hass_with_all_states([whole_house])
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert single == ["sensor.nimbus_whole_house_load_signal_forecast"]
    assert summable == []  # a power signal is never summable-per-circuit


def test_discover_battery_solar_grid_signals_are_excluded_from_both_lists():
    for role in (SIGNAL_ROLE_BATTERY, SIGNAL_ROLE_SOLAR, SIGNAL_ROLE_GRID):
        signal = _nimbus_state(
            f"sensor.nimbus_{role}_signal_forecast", SUBENTRY_TYPE_SIGNAL, role
        )
        hass = _hass_with_all_states([signal])
        single, summable = _discover_nimbus_load_forecast_candidates(hass)
        assert single == [], f"role={role} should never be a load-forecast candidate"
        assert summable == []


def test_discover_a_realistic_mixed_install():
    states = [
        _nimbus_state("sensor.nimbus_pool_pump_load_forecast", SUBENTRY_TYPE_LOAD),
        _nimbus_state("sensor.nimbus_hws_l1_load_forecast", SUBENTRY_TYPE_LOAD),
        _nimbus_state(
            "sensor.nimbus_whole_house_signal_forecast",
            SUBENTRY_TYPE_SIGNAL,
            SIGNAL_ROLE_OTHER,
        ),
        _nimbus_state(
            "sensor.nimbus_battery_signal_forecast",
            SUBENTRY_TYPE_SIGNAL,
            SIGNAL_ROLE_BATTERY,
        ),
        _nimbus_state("sensor.nimbus_solver_battery_forecast", None),  # no tag at all
    ]
    states[-1].attributes = {}  # real Solver-output shape: no ATTR_SUBENTRY_TYPE
    hass = _hass_with_all_states(states)
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert set(single) == {
        "sensor.nimbus_pool_pump_load_forecast",
        "sensor.nimbus_hws_l1_load_forecast",
        "sensor.nimbus_whole_house_signal_forecast",
    }
    assert set(summable) == {
        "sensor.nimbus_pool_pump_load_forecast",
        "sensor.nimbus_hws_l1_load_forecast",
    }


def test_discover_degrades_to_empty_lists_never_raises():
    # Same graceful-degradation convention as every other real-live-data
    # helper in this file -- a discovery failure must only ever mean "no
    # restriction", never break the wizard step.
    hass = MagicMock()
    hass.states.async_all = MagicMock(side_effect=RuntimeError("hass not ready"))
    single, summable = _discover_nimbus_load_forecast_candidates(hass)
    assert single == []
    assert summable == []


def test_solver_sources_step_wires_real_discovery_into_the_rendered_form():
    # End-to-end: the step function itself must call discovery and pass
    # its result into the schema, not just the schema function in
    # isolation (already covered above).
    import asyncio

    flow = _make_flow(options={})
    circuit = _nimbus_state("sensor.nimbus_pool_pump_load_forecast", SUBENTRY_TYPE_LOAD)
    flow.hass.states.async_all = lambda domain: [circuit]
    result = asyncio.run(flow.async_step_solver_sources(None))
    assert result["type"] == "form"
    marker = _find_marker(result["data_schema"], CONF_SOLVER_LOAD_FORECAST_SENSOR)
    assert result["data_schema"].schema[marker].config["include_entities"] == [
        "sensor.nimbus_pool_pump_load_forecast"
    ]


def test_init_step_shows_the_forecaster_vs_solver_vs_switchboard_menu():
    import asyncio

    flow = _make_flow(options={})
    result = asyncio.run(flow.async_step_init(None))
    assert result["type"] == "menu"
    assert result["menu_options"] == ["forecaster", "solver_battery", "switchboard"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
