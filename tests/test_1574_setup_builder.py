"""nimbus #1574 stage 1: repair existing gaps from mappings the user already
confirmed (Mark's device contract on #1574).

Replays two real installs (design docs/design/two-step-setup.md §11):

- the **reference household**: everything is configured, so the plan must be
  EMPTY (the "your install sees no change" promise, pinned);
- the **tester's 6 Oct install** (#1526): his empty Solver battery power and
  cross-check are filled from sensors he had already chosen, and his Wh solar
  total is refused.

Units are not in diagnostics, so each test states the units it assumes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_builder as sb

FIX = Path(__file__).resolve().parent / "fixtures" / "setup_builder"


def _options(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))["options"]


def _lookup(states):
    def look(entity_id):
        if entity_id not in states:
            return None
        unit, attrs = states[entity_id]
        return unit, {"unit_of_measurement": unit, **attrs}

    return look


REFERENCE_STATES = {
    "sensor.logger_battery_power": ("kW", {}),
    "sensor.combined_total_dc_power": ("W", {}),
    "sensor.cb_total_combined_power_adjusted_kw": ("kW", {}),
    "sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast": (
        "kW",
        {"source_sensor": "sensor.cb_total_combined_power_adjusted_kw"},
    ),
}

TESTER_STATES = {  # units assumed for the test (W is what Smappee/GoodWe report)
    "sensor.battery_power_1_2": ("W", {}),
    "sensor.solar_production_total": ("Wh", {}),
    "sensor.smappee_consumption_realtime": ("W", {}),
    "sensor.nimbus_smappee_consumption_realtime_forecast": (
        "kW",
        {"source_sensor": "sensor.smappee_consumption_realtime"},
    ),
}


def test_the_reference_household_gets_nothing():
    plan = sb.plan_setup_fills(
        _options("reference_household.json"), _lookup(REFERENCE_STATES)
    )
    assert plan.empty, plan.options
    assert plan.skipped == []


def test_the_testers_gaps_are_filled_and_his_wh_total_refused():
    plan = sb.plan_setup_fills(
        _options("tester_2026_10_06.json"), _lookup(TESTER_STATES)
    )
    assert plan.options == {
        "solver_battery_power_sensor": "sensor.battery_power_1_2",
        "solver_whole_house_cross_check_sensor": "sensor.smappee_consumption_realtime",
    }
    assert any("'Wh', an energy total" in why for _, why in plan.skipped)


def test_after_he_picks_a_power_sensor_solar_is_filled_too():
    options = {
        **_options("tester_2026_10_06.json"),
        "solar_sensor": "sensor.solar_production",
    }
    plan = sb.plan_setup_fills(
        options, _lookup({**TESTER_STATES, "sensor.solar_production": ("W", {})})
    )
    assert plan.options["solver_solar_power_sensor"] == "sensor.solar_production"


def test_never_overwrites_a_set_field():
    options = {
        "battery_sensor": "sensor.batt",
        "solver_battery_power_sensor": "sensor.other",
    }
    assert sb.plan_setup_fills(options, _lookup({"sensor.batt": ("kW", {})})).empty


def test_a_field_the_user_cleared_is_not_refilled():
    options = {
        "battery_sensor": "sensor.batt",
        "setup_builder_done": ["option:solver_battery_power_sensor"],
    }
    assert sb.plan_setup_fills(options, _lookup({"sensor.batt": ("kW", {})})).empty


def test_missing_or_unit_less_sensors_are_skipped_with_a_reason():
    options = {"battery_sensor": "sensor.gone", "solar_sensor": "sensor.nounit"}
    plan = sb.plan_setup_fills(options, _lookup({"sensor.nounit": (None, {})}))
    assert plan.empty
    why = dict(plan.skipped)
    assert "does not exist" in why["Solver battery power sensor"]
    assert "no power unit" in why["Solver solar power sensor"]


def _hass(states):
    hass = MagicMock()

    def get(eid):
        if eid not in states:
            return None
        unit, attrs = states[eid]
        return SimpleNamespace(
            state="1", attributes={"unit_of_measurement": unit, **attrs}
        )

    hass.states.get.side_effect = get
    hass.services.async_call = AsyncMock()
    return hass


def test_apply_writes_options_once_creates_no_power_signal_and_notifies():
    """Mark, #1574: no learned battery/grid forecasts just because a power
    sensor exists. Stage 1 never adds a subentry."""
    options = _options("tester_2026_10_06.json")
    entry = SimpleNamespace(entry_id="e1", options=options, subentries={})
    hass = _hass(TESTER_STATES)
    plan = asyncio.run(sb.async_apply_setup_fills(hass, entry))
    hass.config_entries.async_add_subentry.assert_not_called()
    hass.config_entries.async_update_entry.assert_called_once()
    written = hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert written["solver_battery_power_sensor"] == "sensor.battery_power_1_2"
    assert set(written[sb.CONF_SETUP_BUILDER_DONE]) == set(plan.markers)
    assert (
        written["battery_sensor"] == options["battery_sensor"]
    )  # nothing else touched
    msg = hass.services.async_call.call_args.args[2]["message"]
    assert "nothing was sent to any device" in msg


def test_apply_on_the_reference_household_writes_nothing():
    entry = SimpleNamespace(
        entry_id="e1", options=_options("reference_household.json"), subentries={}
    )
    hass = _hass(REFERENCE_STATES)
    asyncio.run(sb.async_apply_setup_fills(hass, entry))
    hass.config_entries.async_update_entry.assert_not_called()
    hass.services.async_call.assert_not_called()


def test_apply_never_raises():
    entry = SimpleNamespace(
        entry_id="e1", options={"battery_sensor": "sensor.batt"}, subentries={}
    )
    hass = _hass({"sensor.batt": ("kW", {})})
    hass.config_entries.async_update_entry.side_effect = RuntimeError("boom")
    asyncio.run(sb.async_apply_setup_fills(hass, entry))
