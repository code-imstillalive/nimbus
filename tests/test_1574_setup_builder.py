"""nimbus #1574 stage 1: fill setup gaps from sensors the user already chose.

Replays two real installs (design docs/design/two-step-setup.md §11):

- the **reference household**: everything is configured, so the plan must be
  EMPTY -- the "your install sees no change" promise, pinned;
- the **tester's 6 Oct install** (#1526): the gaps he hit (no Battery
  forecast, empty Solver battery power and cross-check) are filled, and his
  Wh solar total is refused, not used.

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
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    CONF_SIGNAL_ROLE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_SOLAR,
    SUBENTRY_TYPE_SIGNAL,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "setup_builder"


def _load(name):
    raw = json.loads((FIX / name).read_text(encoding="utf-8"))
    subs = [
        SimpleNamespace(
            subentry_type=s["subentry_type"], title=s["title"], data=s["data"]
        )
        for s in raw["subentries"]
    ]
    return raw["options"], subs


def _lookup(states):
    def look(entity_id):
        if entity_id not in states:
            return None
        unit, attrs = states[entity_id]
        return unit, {"unit_of_measurement": unit, **attrs}

    return look


# --- the reference household: nothing to do ----------------------------------

REFERENCE_STATES = {
    "sensor.logger_battery_power": ("kW", {"friendly_name": "Logger Battery power"}),
    "sensor.combined_total_dc_power": (
        "W",
        {"friendly_name": "Combined Total DC Power"},
    ),
    "sensor.logger_meter_total_active_power": ("kW", {}),
    "sensor.cb_total_combined_power_adjusted_kw": ("kW", {}),
    "sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast": (
        "kW",
        {"source_sensor": "sensor.cb_total_combined_power_adjusted_kw"},
    ),
}


def test_the_reference_household_gets_nothing():
    """Every Forecaster and Solver power field is set, and every one of those
    sensors already has a Power Signal (role "other"), so stage 1 is a no-op."""
    options, subs = _load("reference_household.json")
    plan = sb.plan_setup_fills(options, subs, _lookup(REFERENCE_STATES))
    assert plan.empty, (plan.signals, plan.options)
    assert plan.skipped == []


# --- the tester's install: his gaps are filled -------------------------------

TESTER_STATES = {  # units assumed for the test (W is what Smappee/GoodWe report)
    "sensor.battery_power_1_2": ("W", {"friendly_name": "Battery Power 1-2"}),
    "sensor.solar_production_total": (
        "Wh",
        {"friendly_name": "Solar production total"},
    ),
    "sensor.smappee_grid_realtime": ("W", {}),
    "sensor.smappee_consumption_realtime": ("W", {}),
    "sensor.nimbus_smappee_consumption_realtime_forecast": (
        "kW",
        {"source_sensor": "sensor.smappee_consumption_realtime"},
    ),
}


def test_the_testers_gaps_are_filled_and_his_wh_total_refused():
    options, subs = _load("tester_2026_10_06.json")
    plan = sb.plan_setup_fills(options, subs, _lookup(TESTER_STATES))

    # a Battery forecast for the battery sensor he set in Forecaster settings
    assert [(s.sensor, s.role) for s in plan.signals] == [
        ("sensor.battery_power_1_2", SIGNAL_ROLE_BATTERY)
    ]
    assert plan.signals[0].title == "Battery Power 1-2"
    # no Grid signal: his Smappee grid sensor already has one
    # the Solver's empty fields, from what he already chose
    assert plan.options == {
        "solver_battery_power_sensor": "sensor.battery_power_1_2",
        "solver_whole_house_cross_check_sensor": "sensor.smappee_consumption_realtime",
    }
    # the Wh total is refused for both the Solar signal and the Solver field
    reasons = " | ".join(f"{w}: {why}" for w, why in plan.skipped)
    assert reasons.count("'Wh', an energy total") == 2, reasons


def test_after_he_picks_a_power_sensor_solar_is_filled_too():
    options, subs = _load("tester_2026_10_06.json")
    options = {**options, "solar_sensor": "sensor.solar_production"}
    states = {**TESTER_STATES, "sensor.solar_production": ("W", {})}
    plan = sb.plan_setup_fills(options, subs, _lookup(states))
    assert ("sensor.solar_production", SIGNAL_ROLE_SOLAR) in [
        (s.sensor, s.role) for s in plan.signals
    ]
    assert plan.options["solver_solar_power_sensor"] == "sensor.solar_production"


# --- the rules -----------------------------------------------------------------


def _one_battery_install(**extra_options):
    options = {"battery_sensor": "sensor.batt", **extra_options}
    states = {"sensor.batt": ("kW", {})}
    return options, [], _lookup(states)


def test_never_overwrites_a_set_field():
    options, subs, look = _one_battery_install(
        solver_battery_power_sensor="sensor.other"
    )
    plan = sb.plan_setup_fills(options, subs, look)
    assert "solver_battery_power_sensor" not in plan.options


def test_matches_existing_signals_by_sensor_not_role():
    options, _, look = _one_battery_install()
    subs = [
        SimpleNamespace(
            subentry_type="power_signal",
            title="x",
            data={"load_sensor": "sensor.batt", "signal_role": "other"},
        )
    ]
    assert sb.plan_setup_fills(options, subs, look).signals == []


def test_a_load_on_the_sensor_also_counts():
    options, _, look = _one_battery_install()
    subs = [
        SimpleNamespace(
            subentry_type="load", title="x", data={"load_sensor": "sensor.batt"}
        )
    ]
    assert sb.plan_setup_fills(options, subs, look).signals == []


def test_something_the_user_removed_is_not_recreated():
    options, subs, look = _one_battery_install(
        setup_builder_done=["signal:sensor.batt", "option:solver_battery_power_sensor"]
    )
    plan = sb.plan_setup_fills(options, subs, look)
    assert plan.empty


def test_one_sensor_named_twice_gets_one_signal():
    options = {
        "battery_sensor": "sensor.batt",
        "solver_battery_power_sensor": "sensor.batt",
    }
    plan = sb.plan_setup_fills(options, [], _lookup({"sensor.batt": ("kW", {})}))
    assert len(plan.signals) == 1


def test_missing_or_unit_less_sensors_are_skipped_with_a_reason():
    options = {"battery_sensor": "sensor.gone", "grid_sensor": "sensor.nounit"}
    plan = sb.plan_setup_fills(options, [], _lookup({"sensor.nounit": (None, {})}))
    assert plan.signals == []
    why = dict(plan.skipped)
    assert "does not exist" in why["Battery forecast for sensor.gone"]
    assert "no power unit" in why["Grid forecast for sensor.nounit"]


# --- applying -------------------------------------------------------------------


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


def test_apply_adds_signals_then_writes_options_once_and_notifies():
    options, subs = _load("tester_2026_10_06.json")
    entry = SimpleNamespace(
        entry_id="e1",
        options=options,
        subentries={str(i): s for i, s in enumerate(subs)},
    )
    hass = _hass(TESTER_STATES)
    calls = []
    hass.config_entries.async_add_subentry.side_effect = lambda e, sub: calls.append(
        ("add", sub)
    )
    hass.config_entries.async_update_entry.side_effect = lambda e, options: (
        calls.append(("options", options))
    )

    # the HA stub's ConfigSubentry discards its arguments; record them instead
    import homeassistant.config_entries as ce

    real = ce.ConfigSubentry
    ce.ConfigSubentry = lambda **kw: SimpleNamespace(**kw)
    try:
        plan = asyncio.run(sb.async_apply_setup_fills(hass, entry))
    finally:
        ce.ConfigSubentry = real

    assert [c[0] for c in calls] == [
        "add",
        "options",
    ]  # subentries first, ONE options write
    sub = calls[0][1]
    assert sub.subentry_type == SUBENTRY_TYPE_SIGNAL
    assert dict(sub.data) == {
        CONF_LOAD_SENSOR: "sensor.battery_power_1_2",
        CONF_SIGNAL_ROLE: SIGNAL_ROLE_BATTERY,
    }
    written = calls[1][1]
    assert written["solver_battery_power_sensor"] == "sensor.battery_power_1_2"
    assert set(written[sb.CONF_SETUP_BUILDER_DONE]) == set(plan.markers)
    assert (
        written["battery_sensor"] == options["battery_sensor"]
    )  # nothing else touched
    msg = hass.services.async_call.call_args.args[2]["message"]
    assert "Battery" in msg and "Nothing you had set was changed" in msg


def test_apply_on_the_reference_household_writes_nothing():
    options, subs = _load("reference_household.json")
    entry = SimpleNamespace(
        entry_id="e1",
        options=options,
        subentries={str(i): s for i, s in enumerate(subs)},
    )
    hass = _hass(REFERENCE_STATES)
    asyncio.run(sb.async_apply_setup_fills(hass, entry))
    hass.config_entries.async_update_entry.assert_not_called()
    hass.config_entries.async_add_subentry.assert_not_called()
    hass.services.async_call.assert_not_called()


def test_apply_never_raises():
    entry = SimpleNamespace(
        entry_id="e1", options={"battery_sensor": "sensor.batt"}, subentries={}
    )
    hass = _hass({"sensor.batt": ("kW", {})})
    hass.config_entries.async_add_subentry.side_effect = RuntimeError("boom")
    asyncio.run(sb.async_apply_setup_fills(hass, entry))
