"""nimbus #1643, runtime half (Mark Purcell, 8 Oct 2026): a power input whose
unit is stated but is not power is REFUSED rather than read as kW, with the
fallback per input that he approved:

- solar / whole-house / grid meter (and the other optional power inputs):
  treated as not configured;
- battery or controllable-load power read for dispatch: no measurement;
- load forecast: the existing missing-forecast path.

No unit at all is still kW (F09's open decision).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import power_input_check as pic
from custom_components.nimbus_load import solver_shared, solver_writer


def test_the_helper_refuses_every_stated_non_power_unit():
    for unit in ("A", "kVA", "VA", "kWh", "Wh", "furlong", "%"):
        assert solver_shared.power_scale_or_none(unit) is None, unit


def test_the_helper_scales_power_and_keeps_no_unit_as_kw():
    assert solver_shared.power_scale_or_none("W") == 0.001
    assert solver_shared.power_scale_or_none("kW") == 1.0
    assert solver_shared.power_scale_or_none("MW") == 1000.0
    assert solver_shared.power_scale_or_none(None) == 1.0
    assert solver_shared.power_scale_or_none("") == 1.0


UNITS = {
    "sensor.pv_amps": "A",
    "sensor.house_kw": "kW",
    "sensor.meter_kva": "kVA",
    "sensor.batt_w": "W",
    "sensor.no_unit": None,
}


def _ha_get(entity_id):
    unit = UNITS[entity_id]
    return {"attributes": {} if unit is None else {"unit_of_measurement": unit}}


def test_optional_power_inputs_with_a_non_power_unit_become_not_configured(caplog):
    pic._WARNED_REFUSED.clear()
    cfg = {
        "solver_solar_power_sensor": "sensor.pv_amps",
        "solver_whole_house_cross_check_sensor": "sensor.house_kw",
        "switchboard_grid_meter_sensor": "sensor.meter_kva",
        "solver_battery_power_sensor": "sensor.batt_w",
        "grid_sensor": "sensor.no_unit",
        "solver_import_price_sensor": "sensor.not_a_power_input",
    }
    with (
        patch.object(solver_writer, "ha_get", _ha_get),
        caplog.at_level(logging.WARNING),
    ):
        out = solver_writer._refuse_non_power_inputs(dict(cfg))
    assert out["solver_solar_power_sensor"] is None
    assert out["switchboard_grid_meter_sensor"] is None
    # Power units and no unit are kept; non-power settings are not touched.
    assert out["solver_whole_house_cross_check_sensor"] == "sensor.house_kw"
    assert out["solver_battery_power_sensor"] == "sensor.batt_w"
    assert out["grid_sensor"] == "sensor.no_unit"
    assert out["solver_import_price_sensor"] == "sensor.not_a_power_input"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "sensor.pv_amps" in messages and "ignored" in messages


def test_an_unreadable_unit_leaves_the_setting_alone():
    def _raise(_e):
        raise OSError("down")

    cfg = {"solver_solar_power_sensor": "sensor.pv_amps"}
    with patch.object(solver_writer, "ha_get", _raise):
        out = solver_writer._refuse_non_power_inputs(dict(cfg))
    assert out == cfg


def test_a_load_forecast_in_amps_takes_the_missing_forecast_path():
    attrs = {
        "unit_of_measurement": "A",
        "forecast": [
            {"time": "2026-10-08T10:00:00+10:00", "value": 5.0},
            {"time": "2026-10-08T10:30:00+10:00", "value": 6.0},
        ],
    }
    fc, _bands, error = solver_writer._validate_and_parse_load_forecast_attrs(
        "sensor.load_amps", attrs
    )
    assert fc is None
    assert error is not None and "not a power unit" in error
