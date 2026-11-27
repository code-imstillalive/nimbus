"""Nimbus's own solar forecaster on real Home Assistant (solar_forecaster_ha.py, the Configure step,
the PV String fields, the sensor, and what the Solver does with it). No network: the weather fetch
is replaced, so the forecaster stays in `learning` (synthetic data only).

One behaviour, in named parts:

1. a PV String saves the owner's tilt and direction, and a cleared field stays cleared;
2. Configure -> Solar forecaster finds the per-MPPT power sensors (not the house or battery ones),
   adds the ticked ones as PV Strings and switches the forecaster on; the sensor appears, says it is
   learning, and the Solver's settings carry how to use it; switched off, all of that goes;
3. switched on with no PV Strings, a Repair says so;
4. the Solver: `replace` plans on it alone where it covers and the other sources fill in beyond;
   `blend` adds it as one more member; with no forecast at all, nothing changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import solar_forecaster_ha
from custom_components.nimbus_load.const import (
    CONF_PV_STRING_AZIMUTH,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_TILT,
    CONF_SOLAR_FORECASTER_ENABLED,
    CONF_SOLAR_FORECASTER_HISTORY_DAYS,
    CONF_SOLAR_FORECASTER_USE,
    DOMAIN,
    SUBENTRY_TYPE_PV_STRING,
)
from custom_components.nimbus_load.solar_forecaster import runner
from custom_components.nimbus_load.solver_inputs import solar as solar_inputs


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (solar forecaster)", data={}, options=options or {}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _power(hass: HomeAssistant, platform: str, object_id: str, name: str) -> str:
    reg = er.async_get(hass).async_get_or_create(
        "sensor", platform, object_id, suggested_object_id=object_id
    )
    hass.states.async_set(
        reg.entity_id,
        "1.2",
        {"unit_of_measurement": "kW", "device_class": "power", "friendly_name": name},
    )
    return reg.entity_id


async def test_solar_forecaster_from_setup_to_the_solver(hass: HomeAssistant) -> None:
    no_net = patch.object(runner, "fetch_weather", return_value=runner.FetchReport())
    with no_net:
        # part 1: a PV String keeps the owner's tilt and direction; cleared, it stays cleared
        entry = await _setup(hass)
        wall = _power(hass, "inverter", "inv_2_mppt1_power", "Inverter 2 MPPT1 power")
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_PV_STRING), context={"source": "user"}
        )
        flow = await hass.config_entries.subentries.async_configure(
            flow["flow_id"],
            {
                CONF_PV_STRING_ENTITY: wall,
                CONF_PV_STRING_TILT: 90,
                CONF_PV_STRING_AZIMUTH: 0,
            },
        )
        assert flow["type"] is FlowResultType.CREATE_ENTRY
        (sub,) = [
            s
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_PV_STRING
        ]
        assert (sub.data[CONF_PV_STRING_TILT], sub.data[CONF_PV_STRING_AZIMUTH]) == (
            90,
            0,
        )
        flow = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
        await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {CONF_PV_STRING_ENTITY: wall}
        )
        await hass.async_block_till_done()
        assert CONF_PV_STRING_TILT not in entry.subentries[sub.subentry_id].data

        # part 2: Configure -> Solar forecaster: find, tick, switch on
        roof1 = _power(hass, "inverter", "inv_1_mppt1_power", "Inverter 1 MPPT1 power")
        roof2 = _power(hass, "inverter", "inv_1_mppt2_power", "Inverter 1 MPPT2 power")
        idle = _power(hass, "inverter", "inv_1_mppt3_power", "Inverter 1 MPPT3 power")
        hass.states.async_set(
            idle, "0", {"unit_of_measurement": "kW", "device_class": "power"}
        )
        _power(
            hass, "inverter", "inv_1_battery_power", "Battery power"
        )  # never an array
        _power(
            hass, "template", "pv1_mppt_copy_power", "PV1 MPPT copy"
        )  # a template copy loses to the device's own
        result = await hass.config_entries.options.async_init(entry.entry_id)
        if (
            result["type"] is FlowResultType.ABORT
        ):  # restoring right after setup: open it again
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "solar_forecaster" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "solar_forecaster"}
        )
        assert result["step_id"] == "solar_forecaster"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SOLAR_FORECASTER_ENABLED: True,
                CONF_SOLAR_FORECASTER_USE: "blend",
                CONF_SOLAR_FORECASTER_HISTORY_DAYS: 14,
                "find_panels": True,
            },
        )
        assert result["step_id"] == "solar_forecaster_find"
        offered = [
            o["value"] for o in result["data_schema"].schema["arrays"].config["options"]
        ]
        assert set(offered) == {
            roof1,
            roof2,
            idle,
        }  # not the battery, not the template copy, not the one already added
        (field,) = [k for k in result["data_schema"].schema if str(k) == "arrays"]
        assert set(field.default()) == {
            roof1,
            roof2,
        }  # an input with no output: offered, not ticked
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"arrays": [roof1, roof2]}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        strings = {
            s.data[CONF_PV_STRING_ENTITY]
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_PV_STRING
        }
        assert strings == {wall, roof1, roof2}
        assert entry.options[CONF_SOLAR_FORECASTER_ENABLED] is True
        assert entry.options[CONF_SOLAR_FORECASTER_HISTORY_DAYS] == 14
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get("sensor.nimbus_solar_forecast")
        assert state is not None and state.attributes["status"] in (
            "fetching_history",
            "learning",
        )
        assert len(state.attributes["arrays"]) == 3
        assert "Copernicus/ECMWF" in state.attributes["attribution"]
        cfg = hass.states.get("sensor.nimbus_solver_config").attributes
        assert (cfg["solar_forecaster_use"], cfg["solar_forecaster_sensor"]) == (
            "blend",
            "sensor.nimbus_solar_forecast",
        )
        # switched off: no sensor, nothing handed to the Solver
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_SOLAR_FORECASTER_ENABLED: False}
        )
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert solar_forecaster_ha.runtime_for(entry.entry_id) is None
        assert (
            hass.states.get("sensor.nimbus_solver_config").attributes[
                "solar_forecaster_use"
            ]
            is None
        )

        # part 3: on, but no PV Strings: a Repair says so
        bare = MockConfigEntry(
            domain=DOMAIN,
            title="Nimbus (bare)",
            data={},
            options={CONF_SOLAR_FORECASTER_ENABLED: True},
        )
        bare.add_to_hass(hass)
        await solar_forecaster_ha.async_start(hass, bare)
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, solar_forecaster_ha.ISSUE_NO_ARRAYS
            )
            is not None
        )
        solar_forecaster_ha.async_stop(bare.entry_id)
        await hass.config_entries.async_unload(entry.entry_id)

        # part 3b: other solar forecasts already installed are found by integration and key, not by
        # name (two Forecast.Solar planes summed per day, Solcast's day 3 is two days ahead), and a
        # yardstick is judged on its "tomorrow" total as it stood the evening before
        reg = er.async_get(hass)
        for uid, plat, key in (
            ("p1_tomorrow", "forecast_solar", "energy_production_tomorrow"),
            ("p2_tomorrow", "forecast_solar", "energy_production_tomorrow"),
            ("p1_today", "forecast_solar", "energy_production_today"),
            ("sc_d3", "solcast_solar", "total_kwh_forecast_d3"),
            ("sc_peak", "solcast_solar", "peak_w_tomorrow"),  # not a daily total
        ):
            reg.async_get_or_create(
                "sensor", plat, uid, suggested_object_id=uid, translation_key=key
            )
        found = solar_forecaster_ha.find_yardsticks(hass)
        assert set(found) == {"Forecast.Solar", "Solcast"}
        assert [len(found["Forecast.Solar"][k]) for k in (0, 1)] == [1, 2]
        assert set(found["Solcast"]) == {2}
        points = [(100.0, 40.0), (200.0, 42.5), (300.0, float("nan")), (400.0, 50.0)]
        assert solar_forecaster_ha.value_at(points, 350.0) == 42.5
        assert solar_forecaster_ha.value_at(points, 50.0) is None

    # part 4: the Solver. Three periods; the own forecast covers the first two.
    grid = [
        datetime(2026, 1, 10, 2, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
        for i in range(3)
    ]
    other = (
        np.array([5.0, 5.0, 5.0]),
        np.array([1.0, 1.0, 1.0]),
        np.array([8.0, 8.0, 8.0]),
    )
    own = ([2.0, 2.0, 2.0], [1.5, 1.5, 1.5], [3.0, 3.0, 3.0])
    cfg = {"solar_forecaster_sensor": "sensor.nimbus_solar_forecast"}

    def run(use, end, fetched):
        values, lowers, uppers, flags = (
            [other[0].copy()],
            [other[1].copy()],
            [other[2].copy()],
            [False],
        )
        with (
            patch.object(solar_inputs, "_own_forecast_end", return_value=end),
            patch.object(
                solar_inputs, "_fetch_solar_source_safe", return_value=fetched
            ),
        ):
            solar_inputs._apply_own_solar_forecaster(
                {**cfg, "solar_forecaster_use": use},
                grid,
                3,
                values,
                lowers,
                uppers,
                flags,
            )
        return values, lowers, uppers, flags

    v, _lo, hi, _ = run("replace", grid[2], own)
    assert (
        len(v) == 1 and list(v[0]) == [2.0, 2.0, 5.0] and list(hi[0]) == [3.0, 3.0, 8.0]
    )
    v, *_ = run("blend", grid[2], own)
    assert len(v) == 2 and list(v[1]) == [
        2.0,
        2.0,
        5.0,
    ]  # neutral (the others' value) past its end
    v, *_ = run("replace", None, own)  # no rows yet: unchanged
    assert len(v) == 1 and list(v[0]) == [5.0, 5.0, 5.0]
    v, *_ = run("forecast_only", grid[2], own)
    assert len(v) == 1 and list(v[0]) == [5.0, 5.0, 5.0]
