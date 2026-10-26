"""Setup fills (#1574/#1587): a Solver field left empty is offered, as a
one-click Repair, from a sensor the household already confirmed for the same
quantity; never overwritten, never offered twice, and refused visibly when
the sensor cannot be a power reading (#1837).

Covers what lived in #1526, #1562, #1570, #1587 and the setup-builder files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sb = importlib.import_module("custom_components.nimbus_load.setup_builder")
const = importlib.import_module("custom_components.nimbus_load.const")

BATTERY = const.CONF_SOLVER_BATTERY_POWER_SENSOR
SOLAR = const.CONF_SOLVER_SOLAR_POWER_SENSOR
CROSS = const.CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)

SENSORS = {
    "sensor.battery_w": ("W", {}),
    "sensor.solar_kw": ("kW", {}),
    "sensor.solar_kwh": ("kWh", {}),
    "sensor.bare": (None, {}),
    "sensor.amps": ("A", {}),
    "sensor.house_power": ("W", {}),
    "sensor.house_fc": (
        "kW",
        {
            const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_SIGNAL,
            const.ATTR_SIGNAL_ROLE: const.SIGNAL_ROLE_OTHER,
            "source_sensor": "sensor.house_power",
        },
    ),
    "sensor.circuit_fc": (
        "kW",
        {
            const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_LOAD,
            "source_sensor": "sensor.pool",
        },
    ),
    "sensor.unsourced_fc": (
        "kW",
        {
            const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_SIGNAL,
            const.ATTR_SIGNAL_ROLE: const.SIGNAL_ROLE_OTHER,
        },
    ),
}


def _plan(options, sensors=SENSORS):
    return sb.plan_setup_fills(options, sensors.get)


def test_empty_solver_fields_are_offered_from_the_forecasters_confirmed_sensors():
    # test_empty_solver_fields_are_offered_from_the_forecasters_confirmed_sensors
    plan = _plan(
        {
            const.CONF_BATTERY_SENSOR: "sensor.battery_w",
            const.CONF_SOLAR_SENSOR: "sensor.solar_kw",
            const.CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.house_fc",
        }
    )
    assert plan.options == {
        BATTERY: "sensor.battery_w",
        SOLAR: "sensor.solar_kw",
        CROSS: "sensor.house_power",
    }
    assert plan.skipped == [] and not plan.empty

    # test_a_sensor_that_cannot_be_a_power_reading_is_refused_with_why
    for sensor, reason in [
        ("sensor.solar_kwh", "it reports 'kWh', an energy total, not power"),
        ("sensor.bare", "it reports no power unit"),
        ("sensor.amps", "it reports 'A', not a power unit"),
        ("sensor.nowhere", sb.MISSING),
    ]:
        plan = _plan({const.CONF_SOLAR_SENSOR: sensor})
        assert plan.options == {}
        assert plan.skipped == [sb.Skip(SOLAR, sensor, reason)]
        assert plan.skipped[0].missing is (reason == sb.MISSING)

    # test_the_cross_check_is_offered_only_from_a_whole_house_signal
    for load_fc, skipped in [
        ("sensor.circuit_fc", []),  # a circuit is not the whole house: not offered
        (
            "sensor.unsourced_fc",
            [sb.Skip(CROSS, "sensor.unsourced_fc", "it names no source sensor")],
        ),
        ("sensor.gone_fc", [sb.Skip(CROSS, "sensor.gone_fc", sb.MISSING)]),
    ]:
        plan = _plan({const.CONF_SOLVER_LOAD_FORECAST_SENSOR: load_fc})
        assert plan.options == {} and plan.skipped == skipped

    # test_a_whole_house_signal_whose_source_is_not_power_is_refused
    sensors = {**SENSORS, "sensor.house_power": ("kWh", {})}
    plan = _plan({const.CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.house_fc"}, sensors)
    assert plan.skipped == [
        sb.Skip(
            CROSS, "sensor.house_power", "it reports 'kWh', an energy total, not power"
        )
    ]


# --- as Repairs ----------------------------------------------------------------------


class Hass:
    def __init__(self, options):
        self.entry = SimpleNamespace(entry_id="hub1", options=dict(options))
        self.data = {}
        self.updated = []
        self.states = SimpleNamespace(
            get=lambda eid: (
                SimpleNamespace(
                    state="1200",
                    attributes={
                        "unit_of_measurement": SENSORS[eid][0],
                        **SENSORS[eid][1],
                    },
                )
                if eid in SENSORS
                else None
            )
        )
        self.config_entries = SimpleNamespace(
            async_get_entry=lambda eid: self.entry if eid == "hub1" else None,
            async_update_entry=lambda entry, options: (
                self.updated.append(options),
                setattr(entry, "options", options),
            ),
        )


@pytest.fixture
def repairs(monkeypatch):
    raised, deleted = {}, []
    ir = types.ModuleType("homeassistant.helpers.issue_registry")
    ir.IssueSeverity = SimpleNamespace(WARNING="warning")
    ir.async_create_issue = lambda hass, domain, issue_id, **kw: raised.__setitem__(
        issue_id, kw
    )
    ir.async_delete_issue = lambda hass, domain, issue_id: (
        deleted.append(issue_id),
        raised.pop(issue_id, None),
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", ir)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "issue_registry", ir, raising=False
    )
    return raised, deleted


def test_each_offer_is_a_fixable_repair_showing_the_live_reading_and_the_sign(repairs):
    raised, _ = repairs
    hass = Hass(
        {
            const.CONF_BATTERY_SENSOR: "sensor.battery_w",
            const.CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE: True,
        }
    )
    asyncio.run(sb.async_refresh_setup_fills(hass, hass.entry, now=T0))
    issue = raised[f"hub1_{sb.FIX_FLOW_KIND}_{BATTERY}"]
    assert (
        issue["is_fixable"] is True
        and issue["translation_key"] == "setup_fill_battery_power"
    )
    assert issue["translation_placeholders"]["reading"] == "1200 W"
    assert (
        "**positive** value as **charging**"
        in issue["translation_placeholders"]["sign"]
    )
    assert issue["data"] == {
        "kind": sb.FIX_FLOW_KIND,
        "entry_id": "hub1",
        "target": BATTERY,
        "sensor": "sensor.battery_w",
    }


def test_a_missing_sensor_gets_half_an_hour_to_appear_before_it_is_reported(repairs):
    raised, deleted = repairs
    hass = Hass({const.CONF_SOLAR_SENSOR: "sensor.later"})
    blocked = f"hub1_{sb.KIND_BLOCKED}_{SOLAR}"
    asyncio.run(sb.async_refresh_setup_fills(hass, hass.entry, now=T0))
    assert blocked not in raised
    asyncio.run(
        sb.async_refresh_setup_fills(hass, hass.entry, now=T0 + sb.MISSING_GRACE)
    )
    assert raised[blocked]["translation_placeholders"]["reason"] == sb.MISSING
    SENSORS["sensor.later"] = ("kW", {})
    try:
        asyncio.run(
            sb.async_refresh_setup_fills(
                hass, hass.entry, now=T0 + sb.MISSING_GRACE + timedelta(minutes=1)
            )
        )
    finally:
        del SENSORS["sensor.later"]
    assert blocked in deleted  # it appeared: the refusal goes, the offer comes
    assert f"hub1_{sb.FIX_FLOW_KIND}_{SOLAR}" in raised


def test_a_wrong_kind_of_sensor_is_reported_at_once(repairs):
    raised, _ = repairs
    hass = Hass({const.CONF_SOLAR_SENSOR: "sensor.solar_kwh"})
    asyncio.run(sb.async_refresh_setup_fills(hass, hass.entry, now=T0))
    issue = raised[f"hub1_{sb.KIND_BLOCKED}_{SOLAR}"]
    assert (
        issue["is_fixable"] is False
        and "energy total" in issue["translation_placeholders"]["reason"]
    )


def test_confirming_fills_the_field_once_and_rechecks_first():
    hass = Hass({const.CONF_BATTERY_SENSOR: "sensor.battery_w"})
    assert (
        asyncio.run(sb.async_confirm_setup_fill(hass, "hub1", BATTERY, "sensor.other"))
        is False
    )  # not what was offered
    assert (
        asyncio.run(
            sb.async_confirm_setup_fill(hass, "hub1", BATTERY, "sensor.battery_w")
        )
        is True
    )
    (options,) = hass.updated
    assert options[BATTERY] == "sensor.battery_w"
    assert options[sb.CONF_SETUP_BUILDER_DONE] == [f"option:{BATTERY}"]
    # Now set: a second click changes nothing.
    assert (
        asyncio.run(
            sb.async_confirm_setup_fill(hass, "hub1", BATTERY, "sensor.battery_w")
        )
        is False
    )
    assert asyncio.run(sb.async_confirm_setup_fill(hass, "gone", BATTERY, "x")) is False
    assert (
        asyncio.run(sb.async_confirm_setup_fill(hass, "hub1", "not_a_fill", "x"))
        is False
    )


def test_a_broken_refresh_never_breaks_setup(repairs, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(sb, "plan_setup_fills", boom)
    hass = Hass({})
    assert asyncio.run(sb.async_refresh_setup_fills(hass, hass.entry, now=T0)).empty


def test_the_sign_note_names_the_setting_in_force():
    assert "**discharging**" in sb._sign_note({})
    assert sb._reading("sensor.nowhere", Hass({})) == "no reading"
