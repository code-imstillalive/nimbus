"""nimbus issue #563: real tests for build_extra_batteries() -- the
config surface for #467 stage 1's own `batteries: list[BatteryConfig]`
solver support. Real battery_participant subentries -> real BatteryConfig
objects, additional to (never replacing) the hub's own single "home"
battery.

Same solver_writer._NATIVE_HASS-mocking pattern as test_solver_writer_
controllable_loads.py -- pure Python beyond that one seam.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
import solver_writer
from solver.elements import MIN_CHARGE_DISCHARGE_COST_SPREAD


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_state(value, entity_id: str = "sensor.fake"):
    return SimpleNamespace(entity_id=entity_id, state=value, attributes={})


def _fake_native_hass(subentries: list, states: dict | None = None):
    entry = SimpleNamespace(subentries={s.subentry_id: s for s in subentries})
    # ha_get() reads state.entity_id back out -- give each fake state its
    # own real entity_id (the dict key) rather than a shared placeholder,
    # same shape safe_num()'s own real caller (HA's state machine) has.
    resolved_states = {
        eid: SimpleNamespace(entity_id=eid, state=s.state, attributes=s.attributes)
        for eid, s in (states or {}).items()
    }
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        states=SimpleNamespace(get=lambda eid: resolved_states.get(eid)),
    )


_TESLA_DATA = {
    "battery_participant_name": "ev_m3p",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": "sensor.m3p_t_battery_level",
    "battery_participant_power_sensor": "sensor.sigen_inverter_dc_charger_output_power",
    "battery_participant_power_positive_is_charge": True,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
}


class TestBuildExtraBatteries(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_returns_empty_list_when_not_in_native_mode(self):
        solver_writer._NATIVE_HASS = None
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_zero_subentries_is_a_real_no_op(self):
        solver_writer._NATIVE_HASS = _fake_native_hass([])
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_non_battery_participant_subentries_are_ignored(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "load", {"load_sensor": "sensor.pool"})]
        )
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_builds_a_real_battery_participant_from_its_subentry(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _TESLA_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        batteries = solver_writer.build_extra_batteries()
        self.assertEqual(len(batteries), 1)
        b = batteries[0]
        self.assertEqual(b.name, "ev_m3p")
        self.assertEqual(b.capacity_kwh, 60.0)
        self.assertEqual(b.max_charge_kw, 11.0)
        self.assertEqual(b.max_discharge_kw, 11.0)
        self.assertAlmostEqual(b.min_soc_kwh, 12.0)  # 20% of 60
        self.assertAlmostEqual(b.max_soc_kwh, 57.0)  # 95% of 60
        self.assertAlmostEqual(b.initial_soc_kwh, 33.0)  # 55% of 60
        # geometric split of the round-trip efficiency, same formula as
        # the home battery's own construction in main()
        self.assertAlmostEqual(b.charge_efficiency, (0.92) ** 0.5)
        self.assertAlmostEqual(b.discharge_efficiency, (0.92) ** 0.5)

    def test_charge_cost_plus_discharge_cost_clears_the_wash_trade_floor(self):
        # A real, live incident this test locks in: BatteryConfig.
        # __post_init__ structurally rejects a charge_cost+discharge_cost
        # sum below MIN_CHARGE_DISCHARGE_COST_SPREAD (the HAEO wash-
        # trade-degeneracy guard) -- a naive 0.0/0.0 default here would
        # crash build_extra_batteries() (and therefore every real solve)
        # for EVERY configured battery participant.
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _TESLA_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertGreaterEqual(
            b.charge_cost + b.discharge_cost, MIN_CHARGE_DISCHARGE_COST_SPREAD
        )

    def test_charge_limit_entity_overrides_configured_max_soc_percent(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_charge_limit_entity"] = "number.m3p_t_charge_limit"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                "number.m3p_t_charge_limit": _fake_state("80.0"),
            },
        )
        b = solver_writer.build_extra_batteries()[0]
        # 80% (live entity) overrides the configured 95%.
        self.assertAlmostEqual(b.max_soc_kwh, 48.0)  # 80% of 60

    def test_missing_capacity_kwh_is_skipped_not_crashed(self):
        data = dict(_TESLA_DATA)
        del data["battery_participant_capacity_kwh"]
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_missing_soc_sensor_is_skipped_not_crashed(self):
        data = dict(_TESLA_DATA)
        del data["battery_participant_soc_sensor"]
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)]
        )
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_name_reserved_as_home_is_skipped_not_crashed(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_name"] = "home"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        self.assertEqual(solver_writer.build_extra_batteries(), [])

    def test_duplicate_names_the_second_one_is_skipped(self):
        data2 = dict(_TESLA_DATA)
        data2["battery_participant_soc_sensor"] = "sensor.my_t_battery_level"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry("s1", "battery_participant", _TESLA_DATA),
                _fake_subentry("s2", "battery_participant", data2),
            ],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                "sensor.my_t_battery_level": _fake_state("60.0"),
            },
        )
        batteries = solver_writer.build_extra_batteries()
        self.assertEqual(len(batteries), 1)
        self.assertEqual(batteries[0].name, "ev_m3p")

    def test_multiple_distinct_participants_all_get_built(self):
        m3p = dict(_TESLA_DATA)
        my = dict(_TESLA_DATA)
        my["battery_participant_name"] = "ev_my"
        my["battery_participant_soc_sensor"] = "sensor.my_t_battery_level"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry("s1", "battery_participant", m3p),
                _fake_subentry("s2", "battery_participant", my),
            ],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                "sensor.my_t_battery_level": _fake_state("70.0"),
            },
        )
        batteries = solver_writer.build_extra_batteries()
        self.assertEqual({b.name for b in batteries}, {"ev_m3p", "ev_my"})

    def test_a_glitch_soc_reading_is_clamped_not_crashed(self):
        # Same physical-range clamp discipline as the home battery's own
        # live SoC read in main() -- a real sensor glitch (>100% or
        # negative) must not crash the whole solve cycle.
        data = dict(_TESLA_DATA)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("140.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertEqual(b.initial_soc_kwh, b.capacity_kwh)


if __name__ == "__main__":
    unittest.main()
