"""nimbus #1800: the Solver wizard asks for battery capacity, max charge,
max discharge and grid max import, so a new install plans without editing
number entities by hand. The answers go to the live number entities at the
end of the wizard, never into the config entry's options (one master per
setting, DC-004)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_flows_hub_options import _full_solver_data, _make_flow

from custom_components.nimbus_load.const import (
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
)
from custom_components.nimbus_load.flows import live_numbers


def test_the_battery_step_takes_the_numbers_out_of_the_options_path():
    flow = _make_flow(options={})
    result = asyncio.run(
        flow.async_step_solver_battery(
            {
                CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
                "live_solver_battery_capacity_kwh": 119.7,
                "live_solver_max_charge_kw": 40,
                "live_solver_max_discharge_kw": None,  # blank: keep current
            }
        )
    )
    assert result["step_id"] == "solver_grid"
    assert not any(k.startswith("live_") for k in flow._solver_data)
    assert flow._pending_live_numbers() == {
        "solver_battery_capacity_kwh": 119.7,
        "solver_max_charge_kw": 40.0,
    }


def test_finishing_the_wizard_writes_them_and_saves_no_copy():
    flow = _make_flow(options={})
    flow._solver_data = _full_solver_data()
    flow._pending_live_numbers().update(
        {"solver_battery_capacity_kwh": 119.7, "solver_grid_max_import_kw": 60.0}
    )
    flow.hass.services.async_call = AsyncMock()
    with patch.object(live_numbers, "async_apply", AsyncMock(return_value=[])) as apply:
        result = asyncio.run(
            flow.async_step_solver_sources(
                {
                    CONF_SOLVER_SOLAR_FORECAST_SENSOR: "sensor.solcast",
                    CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.load_fc",
                }
            )
        )
    assert result["type"] == "create_entry"
    apply.assert_awaited_once()
    assert apply.await_args.args[2] == {
        "solver_battery_capacity_kwh": 119.7,
        "solver_grid_max_import_kw": 60.0,
    }
    assert not any(k.startswith("live_") for k in result["data"])
    assert "solver_battery_capacity_kwh" not in result["data"]


def test_a_blank_or_missing_answer_writes_nothing():
    flow = _make_flow(options={})
    flow._solver_data = _full_solver_data()
    flow.hass.services.async_call = AsyncMock()
    with patch.object(live_numbers, "async_apply", AsyncMock(return_value=[])) as apply:
        asyncio.run(
            flow.async_step_solver_sources(
                {
                    CONF_SOLVER_SOLAR_FORECAST_SENSOR: "sensor.solcast",
                    CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.load_fc",
                }
            )
        )
    apply.assert_not_awaited()


def test_each_step_offers_its_own_numbers_within_number_py_bounds():
    from custom_components.nimbus_load import number

    bounds = {d.key: (d.min_value, d.max_value) for d in number._DESCRIPTIONS}
    for step, rows in live_numbers.STEP_NUMBERS.items():
        for key, _unit, lo, hi, _step in rows:
            assert bounds[key] == (lo, hi), (step, key)
    battery = {r[0] for r in live_numbers.STEP_NUMBERS["solver_battery"]}
    assert battery == {
        "solver_battery_capacity_kwh",
        "solver_max_charge_kw",
        "solver_max_discharge_kw",
    }
    assert live_numbers.GRID_IMPORT_FIELD == "live_solver_grid_max_import_kw"
