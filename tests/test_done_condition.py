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


class TestParseDoneWhen(unittest.TestCase):
    """nimbus issue #639: parse_done_when() moved here (unchanged) from
    solver_writer.py's own module-private _parse_done_when -- same
    behaviour, new home."""

    def test_ge_operator(self):
        op_fn, threshold = dc.parse_done_when(">= 60")
        self.assertTrue(op_fn(60.0, threshold))
        self.assertFalse(op_fn(59.9, threshold))

    def test_longer_operators_checked_before_single_char(self):
        # ">= 60" must never be misparsed as "> = 60".
        op_fn, threshold = dc.parse_done_when(">=60")
        self.assertEqual(threshold, 60.0)
        self.assertTrue(op_fn(60.0, threshold))

    def test_lt_operator(self):
        op_fn, threshold = dc.parse_done_when("< 10")
        self.assertTrue(op_fn(9.0, threshold))
        self.assertFalse(op_fn(10.0, threshold))

    def test_malformed_done_when_raises_value_error(self):
        with self.assertRaises(ValueError):
            dc.parse_done_when("sixty")


class TestIsTankDone(unittest.TestCase):
    """nimbus issue #639 (Mark Purcell, live verification): whether the
    tank's own done_when condition is genuinely true right now, as
    distinct from a deferrable load's energy target merely being met --
    the exact ambiguity Mark's report caught (status showing "done (tank
    52 °C)" while the device's own done line reads 60 °C)."""

    def test_below_threshold_is_not_done(self):
        self.assertFalse(dc.is_tank_done(52.0, ">= 60"))

    def test_at_or_above_threshold_is_done(self):
        self.assertTrue(dc.is_tank_done(60.0, ">= 60"))
        self.assertTrue(dc.is_tank_done(61.0, ">= 60"))

    def test_missing_current_temperature_is_unknown(self):
        self.assertIsNone(dc.is_tank_done(None, ">= 60"))

    def test_malformed_done_when_is_unknown_not_a_crash(self):
        self.assertIsNone(dc.is_tank_done(52.0, "sixty"))

    def test_unset_done_when_falls_back_to_setpoint(self):
        self.assertTrue(dc.is_tank_done(60.0, None, setpoint_temperature=60.0))
        self.assertFalse(dc.is_tank_done(52.0, None, setpoint_temperature=60.0))

    def test_unset_done_when_and_no_setpoint_is_unknown(self):
        self.assertIsNone(dc.is_tank_done(52.0, None))

    def test_non_numeric_setpoint_is_unknown_not_a_crash(self):
        self.assertIsNone(dc.is_tank_done(52.0, None, setpoint_temperature="unknown"))


if __name__ == "__main__":
    unittest.main()
