"""A participant's availability entity may be any entity whose states say
whether it is here (22 Oct 2026): a binary_sensor ("on"), a device_tracker
or person ("home"), or a sensor/switch whose available states the household
lists (e.g. a charger's "Charging, Connected"). One helper serves the live
gate, the history mask and diagnostics. A binary_sensor reads exactly as
before."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import availability
import solver_writer
from solver_inputs import extra_batteries as extra_batteries_inputs
from test_solver_writer_battery_participant_history import (
    _EV_DATA,
    YESTERDAY_END,
    YESTERDAY_START,
    _flat_history,
    _patch_history,
    battery_participants_inputs,
)
from test_solver_writer_battery_participants import (
    _TESLA_DATA,
    _fake_native_hass,
    _fake_state,
    _fake_subentry,
)

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
ENTITY = "battery_participant_available_entity"
STATES = "battery_participant_available_states"


class TestTheHelper(unittest.TestCase):
    def test_each_kind_keeps_its_standard_meaning_when_none_are_listed(self):
        for entity, state in (
            ("binary_sensor.x", "on"),
            ("input_boolean.x", "on"),
            ("switch.x", "on"),
            ("device_tracker.car", "home"),
            ("person.raf", "home"),
        ):
            states = availability.available_states(entity, None)
            self.assertTrue(availability.is_available(state, states), entity)
            self.assertFalse(availability.is_available("off", states), entity)

    def test_a_listed_set_is_parsed_and_matched_ignoring_case_and_spaces(self):
        states = availability.available_states(
            "sensor.charger", " Charging , Connected,"
        )
        self.assertEqual(states, {"charging", "connected"})
        self.assertTrue(availability.is_available("CHARGING", states))
        self.assertFalse(availability.is_available("Idle", states))

    def test_a_plain_sensor_with_none_listed_is_never_available(self):
        states = availability.available_states("sensor.charger", "")
        self.assertEqual(states, frozenset())
        self.assertFalse(availability.is_available("Charging", states))

    def test_missing_unknown_and_unavailable_are_never_available(self):
        states = availability.available_states("binary_sensor.x", None)
        for state in (None, "unknown", "unavailable"):
            self.assertFalse(availability.is_available(state, states))


class TestTheLiveGate(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig

    def _available(self, entity, state, listed=None):
        data = dict(_TESLA_DATA, **{ENTITY: entity})
        if listed is not None:
            data[STATES] = listed
        solver_writer.NATIVE.hass = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                entity: _fake_state(state),
            },
        )
        return extra_batteries_inputs.build_extra_batteries()[0].available

    def test_a_binary_sensor_reads_exactly_as_before(self):
        self.assertTrue(self._available("binary_sensor.home", "on"))
        self.assertFalse(self._available("binary_sensor.home", "off"))

    def test_a_device_tracker_at_home_is_available(self):
        self.assertTrue(self._available("device_tracker.car", "home"))
        self.assertFalse(self._available("device_tracker.car", "not_home"))

    def test_a_charger_sensor_uses_its_listed_states(self):
        listed = "Charging, Connected"
        self.assertTrue(self._available("sensor.charger", "Charging", listed))
        self.assertTrue(self._available("sensor.charger", "connected", listed))
        self.assertFalse(self._available("sensor.charger", "Disconnected", listed))
        self.assertFalse(self._available("sensor.charger", "Charging"))  # none listed


class TestTheHistoryMask(unittest.TestCase):
    """The same states decide which past hours the participant was here."""

    def setUp(self):
        self._orig = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig

    def test_a_device_tracker_away_hours_are_masked(self):
        data = dict(_EV_DATA, **{ENTITY: "device_tracker.car"})
        solver_writer.NATIVE.hass = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return [(YESTERDAY_START + timedelta(hours=h), 5.0) for h in range(24)]
            return []

        def fetch_state(entity_id, start, end):
            return [
                (
                    YESTERDAY_START + timedelta(hours=h),
                    "not_home" if 8 <= h < 11 else "home",
                )
                for h in range(24)
            ]

        with (
            _patch_history(fetch),
            patch.object(
                solver_writer,
                "fetch_entity_state_history_range",
                side_effect=fetch_state,
            ),
        ):
            results = battery_participants_inputs._resolve_battery_participant_history(
                day_start=YESTERDAY_START,
                day_end=YESTERDAY_END,
                grid_times=[YESTERDAY_START + timedelta(hours=i) for i in range(24)],
                period_hours=1.0,
                n_periods=24,
            )
        _, charge_kw, _discharge_kw, _, _ = results[0]
        for h in range(24):
            self.assertEqual(charge_kw[h], 0.0 if 8 <= h < 11 else 5.0, h)


class TestDiagnosticsAndText(unittest.TestCase):
    def test_diagnostics_reads_the_same_states(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _ha_stubs import install_ha_stubs

        install_ha_stubs()
        from custom_components.nimbus_load import diagnostics

        hass = MagicMock()
        hass.states.get.return_value = _fake_state("home")
        result = diagnostics._battery_participant_diagnostics(
            hass,
            {ENTITY: "device_tracker.car", "battery_participant_soc_sensor": "s.soc"},
        )
        self.assertIs(result["available"], True)

    def test_the_field_has_its_label_and_help_text(self):
        for name in ("strings.json", "translations/en.json"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(f'"{STATES}"', text, name)
            json.loads(text)


if __name__ == "__main__":
    unittest.main()
