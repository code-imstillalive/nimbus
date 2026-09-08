"""nimbus issue #590: real tests for done_condition.py -- the shared
water_heater/climate current_temperature read factored out of
solver_writer.py's own _evaluate_done_condition() (#534) so sensor.py's
schedule-view sensors (#590) can reuse the identical logic without
importing solver_writer.py itself (see that module's own top docstring
for why -- numpy/highspy imports make solver_writer.py unsafe to import
from a plain event-loop context before the first real solve).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load")
)
import done_condition as dc


def _hass(states: dict) -> SimpleNamespace:
    return SimpleNamespace(states=SimpleNamespace(get=lambda eid: states.get(eid)))


class TestReadCurrentTemperature(unittest.TestCase):
    def test_water_heater_reads_current_temperature_attribute(self):
        hass = _hass(
            {
                "water_heater.hws_l1": SimpleNamespace(
                    state="eco", attributes={"current_temperature": 53.8}
                )
            }
        )
        self.assertEqual(dc.read_current_temperature(hass, "water_heater.hws_l1"), 53.8)

    def test_climate_domain_also_reads_current_temperature(self):
        hass = _hass(
            {
                "climate.zone1": SimpleNamespace(
                    state="heat", attributes={"current_temperature": 21.5}
                )
            }
        )
        self.assertEqual(dc.read_current_temperature(hass, "climate.zone1"), 21.5)

    def test_unrecognized_domain_returns_none(self):
        hass = _hass({"sensor.tank_temp": SimpleNamespace(state="60.0", attributes={})})
        self.assertIsNone(dc.read_current_temperature(hass, "sensor.tank_temp"))

    def test_missing_entity_returns_none(self):
        hass = _hass({})
        self.assertIsNone(
            dc.read_current_temperature(hass, "water_heater.does_not_exist")
        )

    def test_unavailable_entity_returns_none(self):
        hass = _hass(
            {
                "water_heater.hws_l1": SimpleNamespace(
                    state="unavailable", attributes={"current_temperature": 53.8}
                )
            }
        )
        self.assertIsNone(dc.read_current_temperature(hass, "water_heater.hws_l1"))

    def test_missing_attribute_returns_none(self):
        hass = _hass(
            {"water_heater.hws_l1": SimpleNamespace(state="eco", attributes={})}
        )
        self.assertIsNone(dc.read_current_temperature(hass, "water_heater.hws_l1"))

    def test_non_numeric_attribute_returns_none_rather_than_raising(self):
        hass = _hass(
            {
                "water_heater.hws_l1": SimpleNamespace(
                    state="eco", attributes={"current_temperature": "unknown"}
                )
            }
        )
        self.assertIsNone(dc.read_current_temperature(hass, "water_heater.hws_l1"))


if __name__ == "__main__":
    unittest.main()
