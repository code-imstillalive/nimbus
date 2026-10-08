"""nimbus #1643: a power input whose unit is stated but is not power (A, kVA,
or unknown) was read as kW, silently, on the Solver's paths.

DC-000's F09 fixture recorded it as a candidate defect; Mark Purcell ruled it
a defect on #1600 (8 Oct 2026). These tests pin what replaces the silence: a
Repair that names each such setting, and a once-per-entity warning on the
Solver's shared read. No unit at all stays kW by design (F09 open_decision),
and an energy unit on a Forecaster/Load/Signal input stays #1562's report.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import power_input_check as pic
from custom_components.nimbus_load import setup_health as sh
from custom_components.nimbus_load import solver_shared

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

UNITS = {
    "sensor.ct_amps": "A",
    "sensor.inverter_kva": "kVA",
    "sensor.odd": "furlong",
    "sensor.power_w": "W",
    "sensor.power_kw": "kW",
    "sensor.energy": "kWh",
    "sensor.no_unit": None,
}


def _hass(units=UNITS) -> MagicMock:
    hass = MagicMock()

    def _get(entity_id):
        if entity_id not in units:
            return None
        unit = units[entity_id]
        attrs = {} if unit is None else {"unit_of_measurement": unit}
        return SimpleNamespace(entity_id=entity_id, state="1.0", attributes=attrs)

    hass.states.get.side_effect = _get
    return hass


def _sub(sub_type, title, **data):
    return SimpleNamespace(subentry_type=sub_type, title=title, data=data)


def _entry(options=None, subentries=()):
    return SimpleNamespace(
        options=options or {},
        subentries={str(i): s for i, s in enumerate(subentries)},
    )


# --- unit recognition --------------------------------------------------------


def test_current_apparent_power_and_unknown_units_are_not_power():
    for unit in ("A", "kVA", "VA", "furlong", "%", " A "):
        assert pic.is_non_power_unit(unit), unit


def test_power_energy_and_no_unit_are_not_flagged():
    # Power is fine; energy is #1562's report; no unit is kW by design.
    for unit in ("W", "kW", "MW", " kW ", "kWh", "Wh", "", "  ", None, 5):
        assert not pic.is_non_power_unit(unit), unit


# --- finding configured inputs ------------------------------------------------


def test_solver_fields_report_non_power_and_energy_units():
    found = pic.find_non_power_unit_inputs(
        _hass(),
        _entry(
            {
                "solver_battery_power_sensor": "sensor.ct_amps",
                "solver_solar_power_sensor": "sensor.energy",
                "solver_whole_house_cross_check_sensor": "sensor.power_w",
                "switchboard_grid_meter_sensor": "sensor.inverter_kva",
                "solver_load_forecast_sensor": "sensor.no_unit",
            }
        ),
    )
    assert sorted(found) == sorted(
        [
            ("Solver settings: battery power sensor", "sensor.ct_amps", "A"),
            ("Solver settings: solar power sensor", "sensor.energy", "kWh"),
            ("Topology: grid meter", "sensor.inverter_kva", "kVA"),
        ]
    )


def test_forecaster_fields_leave_energy_units_to_1562():
    found = pic.find_non_power_unit_inputs(
        _hass(),
        _entry({"grid_sensor": "sensor.energy", "battery_sensor": "sensor.ct_amps"}),
    )
    assert found == [("Forecaster: battery sensor", "sensor.ct_amps", "A")]


def test_subentry_power_sensors_are_checked():
    found = pic.find_non_power_unit_inputs(
        _hass(),
        _entry(
            subentries=[
                _sub(
                    "controllable_load",
                    "Pool Pump",
                    controllable_load_power_sensor="sensor.inverter_kva",
                ),
                _sub(
                    "battery_participant",
                    "EV",
                    battery_participant_power_sensor="sensor.energy",
                ),
                _sub("load", "Laundry", load_sensor="sensor.odd"),
                _sub("load", "Fridge", load_sensor="sensor.energy"),  # 1562's
                _sub("power_signal", "Grid", load_sensor="sensor.power_kw"),
            ]
        ),
    )
    assert sorted(found) == sorted(
        [
            ("Controllable load 'Pool Pump'", "sensor.inverter_kva", "kVA"),
            ("Battery 'EV'", "sensor.energy", "kWh"),
            ("Load 'Laundry'", "sensor.odd", "furlong"),
        ]
    )


def test_unconfigured_and_not_yet_loaded_sensors_are_not_reported():
    found = pic.find_non_power_unit_inputs(
        _hass(),
        _entry({"solver_battery_power_sensor": "sensor.missing", "grid_sensor": ""}),
    )
    assert found == []


# --- the Repair ---------------------------------------------------------------


def test_the_repair_names_every_input():
    issues = sh.evaluate_health(
        options={
            "solver_battery_soc_sensor": "sensor.soc",
            "solver_load_forecast_sensor": "sensor.load_fc",
            "solver_solar_forecast_sensor": "sensor.pv_fc",
            "solver_import_price_sensor": "sensor.buy",
            "solver_export_price_sensor": "sensor.sell",
        },
        forecasts=[],
        hub_up_for=sh.TRAIN_GRACE,
        non_power_unit_inputs=[
            ("Solver settings: battery power sensor", "sensor.ct_amps", "A")
        ],
    )
    assert [i.kind for i in issues] == [sh.KIND_NON_POWER_UNIT]
    assert issues[0].placeholders["inputs"] == (
        "- Solver settings: battery power sensor: `sensor.ct_amps` reports A"
    )


def test_the_repair_has_its_text_in_every_translation():
    for path in (ROOT / "strings.json", ROOT / "translations" / "en.json"):
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        text = issues[sh.KIND_NON_POWER_UNIT]
        assert text["title"] and "{inputs}" in text["description"], path


# --- the Solver's shared read -------------------------------------------------


def test_the_shared_read_warns_once_and_keeps_its_scale(caplog):
    pic._WARNED_NON_POWER.clear()
    state = {"attributes": {"unit_of_measurement": "A"}}
    with (
        patch.object(solver_shared, "ha_get", lambda _e: state),
        caplog.at_level(logging.WARNING),
    ):
        first = solver_shared._kw_scale_factor("sensor.ct_amps_once")
        second = solver_shared._kw_scale_factor("sensor.ct_amps_once")
    assert first == second == 1.0  # unchanged: the plan is not altered here
    warnings = [r for r in caplog.records if "sensor.ct_amps_once" in r.getMessage()]
    assert len(warnings) == 1
    assert "not a power unit" in warnings[0].getMessage()


def test_the_shared_read_is_silent_for_power_units(caplog):
    pic._WARNED_NON_POWER.clear()
    with (
        patch.object(
            solver_shared,
            "ha_get",
            lambda _e: {"attributes": {"unit_of_measurement": "W"}},
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert solver_shared._kw_scale_factor("sensor.power_w_quiet") == 0.001
    assert not [r for r in caplog.records if "power_w_quiet" in r.getMessage()]
