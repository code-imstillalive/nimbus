"""A controllable load's live tuning entities overlay its saved settings (#645)
(#1837).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import solver_writer
from solver_inputs import controllable_loads


def _fake_state(value: str | None) -> SimpleNamespace | None:
    if value is None:
        return None
    return SimpleNamespace(state=value)


def _hass(states: dict) -> SimpleNamespace:
    return SimpleNamespace(states=SimpleNamespace(get=lambda eid: states.get(eid)))


class TestNativeModeOnly(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig

    def test_standalone_mode_returns_data_unchanged(self):
        solver_writer.NATIVE.hass = None
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = controllable_loads._resolve_controllable_load_tuning(data, subentry)
        self.assertIs(result, data)


class TestLiveOverlay(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig

    def test_all_seven_live_fields_can_be_overridden_independently(self):
        slug = "hot_water_heat_pump"
        solver_writer.NATIVE.hass = _hass(
            {
                f"number.nimbus_{slug}_deferrable_target_kwh": _fake_state("1.1"),
                f"number.nimbus_{slug}_deferrable_max_power_kw": _fake_state("2.2"),
                f"number.nimbus_{slug}_deferrable_earliest_hour": _fake_state("3.3"),
                f"number.nimbus_{slug}_deferrable_deadline_hour": _fake_state("4.4"),
                f"number.nimbus_{slug}_deferrable_shortfall_price": _fake_state("5.5"),
                f"number.nimbus_{slug}_controllable_load_min_hold_minutes": _fake_state(
                    "6.6"
                ),
                f"number.nimbus_{slug}_controllable_load_max_activations_per_day": _fake_state(
                    "7"
                ),
            }
        )
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = controllable_loads._resolve_controllable_load_tuning({}, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 1.1)
        self.assertEqual(result["deferrable_max_power_kw"], 2.2)
        self.assertEqual(result["deferrable_earliest_hour"], 3.3)
        self.assertEqual(result["deferrable_deadline_hour"], 4.4)
        self.assertEqual(result["deferrable_shortfall_price"], 5.5)
        self.assertEqual(result["controllable_load_min_hold_minutes"], 6.6)
        self.assertEqual(result["controllable_load_max_activations_per_day"], 7.0)

    def test_non_numeric_live_state_falls_back_to_wizard_data(self):
        """Defensive against a genuinely corrupt/mid-transition state --
        must never crash the whole load's own solve over one bad read."""
        solver_writer.NATIVE.hass = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "not-a-number"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = controllable_loads._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)


if __name__ == "__main__":
    unittest.main()
