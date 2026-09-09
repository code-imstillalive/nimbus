"""Direct test coverage for nimbus issue #645: solver_writer.
_resolve_controllable_load_tuning() -- overlays a Controllable Load's 7
real live-editable tuning fields (number.nimbus_<load>_<key>) on top of
its own wizard-saved subentry.data, mirroring number.py's hub-level
"wizard for first-time setup, live entity for day-to-day tuning" split.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
import solver_writer


def _fake_state(value: str | None) -> SimpleNamespace | None:
    if value is None:
        return None
    return SimpleNamespace(state=value)


def _hass(states: dict) -> SimpleNamespace:
    return SimpleNamespace(states=SimpleNamespace(get=lambda eid: states.get(eid)))


class TestNativeModeOnly(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig

    def test_standalone_mode_returns_data_unchanged(self):
        solver_writer._NATIVE_HASS = None
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertIs(result, data)


class TestLiveOverlay(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig

    def test_no_live_entities_configured_falls_back_to_wizard_data(self):
        solver_writer._NATIVE_HASS = _hass({})
        data = {
            "deferrable_target_kwh": 2.0,
            "deferrable_max_power_kw": 0.65,
            "controllable_load_device_entity": "water_heater.hws",
        }
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)
        self.assertEqual(result["deferrable_max_power_kw"], 0.65)
        # A non-tunable field (device_entity) is carried through untouched.
        self.assertEqual(result["controllable_load_device_entity"], "water_heater.hws")

    def test_live_number_overrides_the_wizard_value(self):
        solver_writer._NATIVE_HASS = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "3.5"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 3.5)

    def test_all_seven_live_fields_can_be_overridden_independently(self):
        slug = "hot_water_heat_pump"
        solver_writer._NATIVE_HASS = _hass(
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
        result = solver_writer._resolve_controllable_load_tuning({}, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 1.1)
        self.assertEqual(result["deferrable_max_power_kw"], 2.2)
        self.assertEqual(result["deferrable_earliest_hour"], 3.3)
        self.assertEqual(result["deferrable_deadline_hour"], 4.4)
        self.assertEqual(result["deferrable_shortfall_price"], 5.5)
        self.assertEqual(result["controllable_load_min_hold_minutes"], 6.6)
        self.assertEqual(result["controllable_load_max_activations_per_day"], 7.0)

    def test_unavailable_live_entity_falls_back_to_wizard_data(self):
        solver_writer._NATIVE_HASS = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "unavailable"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)

    def test_unknown_live_entity_falls_back_to_wizard_data(self):
        solver_writer._NATIVE_HASS = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "unknown"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)

    def test_non_numeric_live_state_falls_back_to_wizard_data(self):
        """Defensive against a genuinely corrupt/mid-transition state --
        must never crash the whole load's own solve over one bad read."""
        solver_writer._NATIVE_HASS = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "not-a-number"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)

    def test_missing_wizard_value_and_missing_live_entity_stays_absent(self):
        """Neither source has this field -- must not fabricate a 0.0 or
        any other default; the caller's own existing `.get(key)` default
        handling stays exactly as it was before #645."""
        solver_writer._NATIVE_HASS = _hass({})
        subentry = SimpleNamespace(title="Hot Water Heat Pump")
        result = solver_writer._resolve_controllable_load_tuning({}, subentry)
        self.assertNotIn("deferrable_target_kwh", result)

    def test_missing_title_fails_open_to_data_unchanged(self):
        """A real ConfigSubentry always has .title -- but a genuinely
        unusual/malformed object here must fail open rather than crash
        this load's own solve over a slug it can't compute."""
        solver_writer._NATIVE_HASS = _hass(
            {
                "number.nimbus_hot_water_heat_pump_deferrable_target_kwh": _fake_state(
                    "9.9"
                )
            }
        )
        data = {"deferrable_target_kwh": 2.0}
        subentry = SimpleNamespace()  # no title attribute at all
        result = solver_writer._resolve_controllable_load_tuning(data, subentry)
        self.assertEqual(result["deferrable_target_kwh"], 2.0)

    def test_slug_matches_the_same_technique_sensor_py_uses(self):
        """Same entity_id a household would actually see -- proves the
        slug isn't independently reinvented in a way that could drift
        from sensor.py's own commanded_state entity naming for the same
        load."""
        result = solver_writer._slug_for_controllable_load_entity_id(
            "Hot Water Heat Pump (SG Ready)"
        )
        self.assertEqual(result, "hot_water_heat_pump_sg_ready")


if __name__ == "__main__":
    unittest.main()
