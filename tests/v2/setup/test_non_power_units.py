"""A power input whose unit is stated but is not power is refused, never read
as kW, and each input takes its own fallback (#1643) (#1837).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import power_input_check as pic
from custom_components.nimbus_load import solver_writer

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
    # test_optional_power_inputs_with_a_non_power_unit_become_not_configured
    caplog.clear()
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

    # test_an_unreadable_unit_leaves_the_setting_alone
    def _raise(_e):
        raise OSError("down")

    cfg = {"solver_solar_power_sensor": "sensor.pv_amps"}
    with patch.object(solver_writer, "ha_get", _raise):
        out = solver_writer._refuse_non_power_inputs(dict(cfg))
    assert out == cfg
