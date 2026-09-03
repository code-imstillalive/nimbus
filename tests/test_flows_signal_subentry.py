"""Real test of flows/signal_subentry.py -- NimbusSignalSubentryFlowHandler's
real reconfigure-vs-new routing and title-derivation logic (same pattern as
load_subentry.py's own equivalent, this file's own docstring points at that
sibling for the full "why" -- EntitySelector has no null-default crash the
way NumberSelector does, so this form's single field is always safe with a
plain default=, unlike load_subentry.py's schedule/expected-load fields).

Imports and exercises the REAL methods (not a reimplementation) against
real `voluptuous` and tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    CONF_SIGNAL_ROLE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_GRID,
    SIGNAL_ROLE_HUMIDITY,
    SIGNAL_ROLE_OTHER,
    SIGNAL_ROLE_SOLAR,
    SIGNAL_ROLE_TEMPERATURE,
)
from custom_components.nimbus_load.flows.signal_subentry import (
    NimbusSignalSubentryFlowHandler,
)


def _make_flow(source: str = "user") -> NimbusSignalSubentryFlowHandler:
    flow = NimbusSignalSubentryFlowHandler.__new__(NimbusSignalSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    return flow


def test_derive_title_uses_friendly_name_when_present():
    flow = _make_flow()
    flow.hass.states.get.return_value = MagicMock(
        attributes={"friendly_name": "Logger Battery Power"}
    )
    assert flow._derive_title("sensor.battery_power") == "Logger Battery Power"


def test_derive_title_falls_back_to_entity_id_when_no_friendly_name():
    flow = _make_flow()
    flow.hass.states.get.return_value = MagicMock(attributes={})
    assert flow._derive_title("sensor.battery_power") == "sensor.battery_power"


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_user({CONF_LOAD_SENSOR: "sensor.battery_power"})
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_LOAD_SENSOR: "sensor.battery_power"}


def test_reconfigure_source_updates_existing_entry_not_creates_new():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.old_battery"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    flow.hass.states.get.return_value = None
    result = asyncio.run(flow.async_step_user({CONF_LOAD_SENSOR: "sensor.old_battery"}))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"


def test_reconfigure_with_no_input_prefills_form_from_existing_data():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.existing_solar"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    marker = next(k for k in result["data_schema"].schema if k == CONF_LOAD_SENSOR)
    assert marker.default() == "sensor.existing_solar"


def test_fresh_form_defaults_role_to_other():
    # 2026-08-23: the role field can never be genuinely blank (unlike an
    # EntitySelector, "other" is always a valid choice) -- confirms the
    # schema's own default= actually resolves to "other" on a brand new
    # signal, not left unset.
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    marker = next(k for k in result["data_schema"].schema if k == CONF_SIGNAL_ROLE)
    assert marker.default() == SIGNAL_ROLE_OTHER


def test_role_is_preserved_through_a_real_submission():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_user(
            {CONF_LOAD_SENSOR: "sensor.grid_meter", CONF_SIGNAL_ROLE: SIGNAL_ROLE_GRID}
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SIGNAL_ROLE] == SIGNAL_ROLE_GRID


def test_reconfigure_prefills_role_from_existing_data_not_reset_to_other():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(
        data={
            CONF_LOAD_SENSOR: "sensor.battery_power",
            CONF_SIGNAL_ROLE: SIGNAL_ROLE_BATTERY,
        }
    )
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    result = asyncio.run(flow.async_step_user(None))
    marker = next(k for k in result["data_schema"].schema if k == CONF_SIGNAL_ROLE)
    assert marker.default() == SIGNAL_ROLE_BATTERY


def test_role_selector_offers_all_six_real_options():
    # 2026-09-03: was "all four" -- Temperature/Humidity added so a
    # weather-type power signal isn't forced through kW/POWER semantics
    # via SIGNAL_ROLE_OTHER (see const.py's own comment on the two new
    # roles for the real household bug this closes).
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    marker = next(k for k in result["data_schema"].schema if k == CONF_SIGNAL_ROLE)
    selector_instance = result["data_schema"].schema[marker]
    assert selector_instance.config["options"] == [
        SIGNAL_ROLE_OTHER,
        SIGNAL_ROLE_BATTERY,
        SIGNAL_ROLE_SOLAR,
        SIGNAL_ROLE_GRID,
        SIGNAL_ROLE_TEMPERATURE,
        SIGNAL_ROLE_HUMIDITY,
    ]


def test_step_reconfigure_alias_delegates_to_step_user():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.existing"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_reconfigure({CONF_LOAD_SENSOR: "sensor.existing"})
    )
    assert result["type"] == "update_and_abort"


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
