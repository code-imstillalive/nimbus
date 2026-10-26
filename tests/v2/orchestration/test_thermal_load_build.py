"""A thermal controllable load's subentry becomes the solver's
ThermalLoadConfig (#774) (#1837).

Its sensors, band, rates and costs are read from the subentry and live
states; one that cannot be built is skipped with a warning, never a crash.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import load_run_state
import solver_writer
from solver_inputs import controllable_loads

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
        self._orig_native_hass = solver_writer.NATIVE.hass

    def tearDown(self):
        solver_writer.NATIVE.hass = self._orig_native_hass

    def _run(self, data: dict, states: dict, now=None):
        now = now or datetime(2026, 9, 13, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 96)  # 48h of 30-min periods
        solver_writer.NATIVE.hass = _fake_native_hass(
            [_fake_subentry("s1", "controllable_load", data)], states=states
        )
        return solver_writer.build_controllable_loads(now, grid_times, len(grid_times))

    def test_missing_max_power_kw_is_skipped_not_crashed(self):
        # test_missing_max_power_kw_is_skipped_not_crashed
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

        # test_missing_target_temperature_is_skipped_not_crashed
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

        # test_missing_temperature_entity_is_skipped_not_crashed
        data = {
            "controllable_load_name": "HWS",
            "controllable_load_kind": "thermal",
            "thermal_max_power_kw": 3.0,
            "thermal_target_temperature_c": 60.0,
        }
        _sheddable, _adequacy, thermal = self._run(data, states={})
        self.assertEqual(thermal, [])

        # test_a_non_water_heater_climate_temperature_entity_is_skipped
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

        # test_comfort_floor_fields_are_carried_through_when_set
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

        # test_explicit_rate_overrides_win_over_module_defaults
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
        # nimbus issue #940: the origin is recorded on the same branch
        # that picked the value, so it cannot describe a different branch
        # than the one taken.
        self.assertEqual(tl.heating_rate_origin, "override")
        self.assertEqual(tl.idle_decay_origin, "override")

        # test_persisted_run_state_rate_wins_over_module_default_when_no_override
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
        solver_writer.NATIVE.hass = _fake_native_hass(
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
        # nimbus #1302 (spec 003): patched on solver_inputs.controllable_loads,
        # NOT on solver_writer. build_controllable_loads moved into that module
        # and calls _sample_load_run_state as a bare intra-module name, so
        # solver_writer's re-export is no longer the name it reads -- patching
        # the facade here would be a silent no-op and this test would pass on
        # the module default instead of the learned state it means to check.
        orig = controllable_loads._sample_load_run_state
        controllable_loads._sample_load_run_state = lambda *a, **k: learned_state
        try:
            _sheddable, _adequacy, thermal = solver_writer.build_controllable_loads(
                now, grid_times, len(grid_times)
            )
        finally:
            controllable_loads._sample_load_run_state = orig

        tl = thermal[0]
        self.assertEqual(tl.heating_rate_c_per_kwh, 9.4)
        self.assertEqual(tl.idle_decay_c_per_hour, 0.62)
        self.assertEqual(tl.heating_rate_origin, "learned")  # nimbus issue #940
        self.assertEqual(tl.idle_decay_origin, "learned")


if __name__ == "__main__":
    unittest.main()


def test_a_household_mode_moves_the_band_the_solver_receives():
    """#1314: the mode's band preset is applied before the thermal branch
    reads its target and floor, so `away` reaches the LP one setback lower."""
    original = solver_writer.NATIVE.hass
    data = {
        "controllable_load_name": "Hot Water Heat Pump",
        "controllable_load_kind": "thermal",
        "thermal_temperature_entity": "water_heater.hws",
        "thermal_max_power_kw": 3.0,
        "thermal_target_temperature_c": 60.0,
        "thermal_comfort_floor_c": 50.0,
        "thermal_earliest_hour": 6.0,
        "thermal_deadline_hour": 16.0,
    }

    def build(mode):
        sub = _fake_subentry("s1", "controllable_load", data)
        sub.title = "Hot Water Heat Pump"
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=42.0),
            "select.nimbus_household_mode": SimpleNamespace(state=mode, attributes={}),
        }
        solver_writer.NATIVE.hass = _fake_native_hass([sub], states=states)
        now = datetime(2026, 9, 13, 0, 0, tzinfo=_TZ)
        grid = _grid(now, 96)
        (load,) = solver_writer.build_controllable_loads(now, grid, len(grid))[2]
        return load

    try:
        home, away = build("home"), build("away")
    finally:
        solver_writer.NATIVE.hass = original
    assert away.comfort_floor_c == home.comfort_floor_c - 5.0
    assert away.target_temperature_c == home.target_temperature_c - 5.0
