"""nimbus issue #774: real tests for build_controllable_loads()'s own
kind=thermal branch -- ConfigSubentry -> solver.elements.ThermalLoadConfig
resolution. Same _fake_native_hass/_fake_subentry mocking pattern already
established by test_solver_writer_controllable_loads.py/test_solver_writer_
battery_participants.py (each test file keeps its own small copy of these
helpers, not a shared import -- this project's own established convention).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import _solver_path  # noqa: F401
import load_run_state
import solver_writer
import thermal_forecast
from solver.elements import ThermalLoadConfig

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


def _grid(start: datetime, n: int, minutes: int = 30) -> list[datetime]:
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_native_hass(subentries: list, states: dict | None = None):
    entry = SimpleNamespace(
        entry_id="entry_thermal", subentries={s.subentry_id: s for s in subentries}
    )
    fake = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry])
    )
    if states is not None:
        fake.states = SimpleNamespace(get=lambda eid: states.get(eid))
    return fake


def _fake_water_heater_state(mode="eco", current_temperature=None):
    attrs = {}
    if current_temperature is not None:
        attrs["current_temperature"] = current_temperature
    return SimpleNamespace(state=mode, attributes=attrs)


class TestBuildControllableLoadsThermalBranch(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def _run(self, data: dict, states: dict, now=None):
        now = now or datetime(2026, 9, 13, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 96)  # 48h of 30-min periods
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "controllable_load", data)], states=states
        )
        return solver_writer.build_controllable_loads(now, grid_times, len(grid_times))

    def test_builds_a_real_thermal_load_from_its_subentry(self):
        data = {
            "controllable_load_name": "Hot Water Heat Pump",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
            "thermal_earliest_hour": 6.0,
            "thermal_deadline_hour": 16.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        self.assertEqual(len(thermal), 1)
        tl = thermal[0]
        self.assertIsInstance(tl, ThermalLoadConfig)
        self.assertEqual(tl.name, "Hot Water Heat Pump")
        self.assertEqual(tl.max_power_kw, 3.0)
        self.assertEqual(tl.initial_temperature_c, 42.0)
        self.assertEqual(tl.target_temperature_c, 60.0)
        self.assertEqual(tl.subentry_id, "s1")
        # No override/persisted rate available -- module-level defaults.
        self.assertEqual(
            tl.heating_rate_c_per_kwh, thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
        )
        self.assertEqual(
            tl.idle_decay_c_per_hour, thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR
        )

    def test_missing_max_power_kw_is_skipped_not_crashed(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_target_temperature_c": 60.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        self.assertEqual(thermal, [])

    def test_missing_target_temperature_is_skipped_not_crashed(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        self.assertEqual(thermal, [])

    def test_missing_temperature_entity_is_skipped_not_crashed(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        _sheddable, _adequacy, thermal = self._run(data, states={})
        self.assertEqual(thermal, [])

    def test_unavailable_temperature_entity_is_skipped_not_crashed(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        states = {
            "water_heater.hws": SimpleNamespace(state="unavailable", attributes={})
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        self.assertEqual(thermal, [])

    def test_a_non_water_heater_climate_temperature_entity_is_skipped(self):
        # done_condition.read_current_temperature() only supports
        # water_heater/climate domains -- a plain sensor has no
        # current_temperature ATTRIBUTE contract to read.
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "sensor.hws_temp",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        states = {"sensor.hws_temp": SimpleNamespace(state="42.0", attributes={})}
        _sheddable, _adequacy, thermal = self._run(data, states)
        self.assertEqual(thermal, [])

    def test_earliest_and_deadline_hours_resolve_to_real_period_indices(self):
        now = datetime(2026, 9, 13, 0, 0, tzinfo=_TZ)
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
            "thermal_earliest_hour": 6.0,
            "thermal_deadline_hour": 16.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states, now=now)
        tl = thermal[0]
        # 30-min grid from midnight -- 06:00 is period 12, 16:00 is period 32.
        self.assertEqual(tl.earliest_period, 12)
        self.assertEqual(tl.deadline_period, 32)

    def test_no_earliest_deadline_hour_defaults_to_the_whole_horizon(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        tl = thermal[0]
        self.assertEqual(tl.earliest_period, 0)
        self.assertEqual(tl.deadline_period, 95)  # n_periods - 1

    def test_comfort_floor_fields_are_carried_through_when_set(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
            "thermal_comfort_floor_c": 40.0,
            "thermal_comfort_floor_cost": 5.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        tl = thermal[0]
        self.assertEqual(tl.comfort_floor_c, 40.0)
        self.assertEqual(tl.comfort_floor_cost, 5.0)

    def test_no_comfort_floor_defaults_to_a_complete_no_op(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        tl = thermal[0]
        self.assertIsNone(tl.comfort_floor_c)
        self.assertEqual(tl.comfort_floor_cost, 0.0)

    def test_explicit_rate_overrides_win_over_module_defaults(self):
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
            "thermal_heating_rate_c_per_kwh": 12.5,
            "thermal_idle_decay_c_per_hour": 0.75,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0)
        }
        _sheddable, _adequacy, thermal = self._run(data, states)
        tl = thermal[0]
        self.assertEqual(tl.heating_rate_c_per_kwh, 12.5)
        self.assertEqual(tl.idle_decay_c_per_hour, 0.75)

    def test_persisted_run_state_rate_wins_over_module_default_when_no_override(self):
        # A load migrated from kind=deferrable already has a real,
        # previously-learned rate sitting in its own LoadRunState (keyed
        # by subentry_id, not kind) -- read via run_state_sample, which
        # requires a configured power_sensor to be built at all.
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "controllable_load_power_sensor": "sensor.hws_power",
            "thermal_temperature_entity": "water_heater.hws",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0),
            "sensor.hws_power": SimpleNamespace(
                state="0.0", attributes={"unit_of_measurement": "kW"}
            ),
        }
        now = datetime(2026, 9, 13, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 96)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "controllable_load", data)], states=states
        )

        # Monkeypatch _sample_load_run_state to hand back a fixed,
        # already-learned LoadRunState -- the real Store round-trip is
        # covered by TestSampleLoadRunState elsewhere; this test only
        # cares that build_controllable_loads() reads the two thermal
        # rate fields off whatever it gets back.
        learned_state = load_run_state.LoadRunState(
            day_key="2026-09-13",
            thermal_heating_rate_c_per_kwh=9.4,
            thermal_idle_decay_c_per_hour=0.62,
        )
        orig = solver_writer._sample_load_run_state
        solver_writer._sample_load_run_state = lambda *a, **k: learned_state
        try:
            _sheddable, _adequacy, thermal = solver_writer.build_controllable_loads(
                now, grid_times, len(grid_times)
            )
        finally:
            solver_writer._sample_load_run_state = orig

        tl = thermal[0]
        self.assertEqual(tl.heating_rate_c_per_kwh, 9.4)
        self.assertEqual(tl.idle_decay_c_per_hour, 0.62)


if __name__ == "__main__":
    unittest.main()
