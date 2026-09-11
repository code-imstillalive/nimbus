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


class TestAvailabilityGateWiring(unittest.TestCase):
    """nimbus issue #563 item 2: build_extra_batteries() reading a live
    available_entity into BatteryConfig.available. The LP mechanics
    themselves (ub=0.0 for a whole solve) are covered by
    test_solver_battery_participant_gating_and_shared_charger.py -- this
    file only proves the config-surface -> BatteryConfig wiring."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_no_available_entity_configured_defaults_available_true(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", dict(_TESLA_DATA))],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertTrue(b.available)

    def test_available_entity_on_means_available(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_available_entity"] = (
            "binary_sensor.m3p_t_located_at_home"
        )
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                "binary_sensor.m3p_t_located_at_home": _fake_state("on"),
            },
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertTrue(b.available)

    def test_available_entity_off_means_unavailable(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_available_entity"] = (
            "binary_sensor.m3p_t_located_at_home"
        )
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={
                "sensor.m3p_t_battery_level": _fake_state("55.0"),
                "binary_sensor.m3p_t_located_at_home": _fake_state("off"),
            },
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertFalse(b.available)

    def test_missing_available_entity_state_is_conservatively_unavailable(self):
        # Real case: the configured entity_id doesn't currently resolve
        # to any state (renamed, integration reloading) -- must not
        # crash, and must be treated as NOT available (the conservative
        # reading for a live safety-relevant gate), not silently True.
        data = dict(_TESLA_DATA)
        data["battery_participant_available_entity"] = "binary_sensor.does_not_exist"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertFalse(b.available)


class TestDepartureDeadlineWiring(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def _periods(self, start_hour: int, n: int):
        from datetime import UTC, datetime

        import numpy as np
        from solver.elements import PeriodGrid

        return PeriodGrid(
            hours=np.array([1.0] * n),
            start=datetime(2026, 9, 8, start_hour, 0, tzinfo=UTC),
        )

    def test_both_fields_set_resolves_to_a_real_period_index(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_departure_hour"] = 8
        data["battery_participant_must_have_soc_by_departure_percent"] = 90.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        periods = self._periods(start_hour=6, n=6)  # 06:00..11:00, hour 8 is index 2
        b = solver_writer.build_extra_batteries(periods)[0]
        self.assertEqual(b.must_have_soc_by_period_index, 2)
        self.assertAlmostEqual(b.must_have_soc_kwh, 60.0 * 0.90)

    def test_no_matching_hour_in_horizon_is_a_silent_no_op(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_departure_hour"] = 20
        data["battery_participant_must_have_soc_by_departure_percent"] = 90.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        periods = self._periods(
            start_hour=6, n=4
        )  # 06:00..09:00, hour 20 never appears
        b = solver_writer.build_extra_batteries(periods)[0]
        self.assertIsNone(b.must_have_soc_by_period_index)
        self.assertIsNone(b.must_have_soc_kwh)

    def test_only_departure_hour_set_is_treated_as_neither_set(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_departure_hour"] = 8
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        periods = self._periods(start_hour=6, n=6)
        b = solver_writer.build_extra_batteries(periods)[0]
        self.assertIsNone(b.must_have_soc_by_period_index)
        self.assertIsNone(b.must_have_soc_kwh)

    def test_no_periods_argument_is_a_real_no_op(self):
        # Every pre-#563-items-2/3 caller (and every other test in this
        # file) calls build_extra_batteries() with zero arguments --
        # must keep working exactly as before.
        data = dict(_TESLA_DATA)
        data["battery_participant_departure_hour"] = 8
        data["battery_participant_must_have_soc_by_departure_percent"] = 90.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertIsNone(b.must_have_soc_by_period_index)
        self.assertIsNone(b.must_have_soc_kwh)


class TestSharedChargerWiring(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_group_and_ceiling_pass_through_to_batteryconfig(self):
        data = dict(_TESLA_DATA)
        data["battery_participant_shared_charger_group"] = "dc_charger"
        data["battery_participant_shared_charger_max_kw"] = 25.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertEqual(b.shared_charger_group, "dc_charger")
        self.assertEqual(b.shared_charger_max_kw, 25.0)

    def test_ungrouped_by_default(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", dict(_TESLA_DATA))],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        b = solver_writer.build_extra_batteries()[0]
        self.assertIsNone(b.shared_charger_group)
        self.assertIsNone(b.shared_charger_max_kw)


class TestOutOfRangeSocWarningParity(unittest.TestCase):
    """Mark Purcell's own live-tested #563 review flagged a real
    asymmetry: the home battery's own live SoC read in main() logs a
    WARNING when it sits outside [min, max]; build_extra_batteries()
    didn't. Proves the fix -- doesn't crash, doesn't clamp away the real
    value (soft overfill/underfill slack still handles it downstream),
    just now also logs."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        # nimbus issue #601: this suite's own module-level warn-once
        # tracking now persists across calls (previously every call
        # warned unconditionally, so a leaked key from another test
        # class never mattered) -- isolate it here too.
        solver_writer._BATTERY_PARTICIPANT_WARNED.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        solver_writer._BATTERY_PARTICIPANT_WARNED.clear()

    def test_soc_above_configured_ceiling_logs_a_warning(self):
        data = dict(_TESLA_DATA)  # max_soc_percent=95.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("100.0")},
        )
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as cm:
            b = solver_writer.build_extra_batteries()[0]
        self.assertTrue(
            any("outside its own configured floor/ceiling" in msg for msg in cm.output)
        )
        # Real value passes through honestly -- no silent clamp-and-pretend.
        self.assertAlmostEqual(b.initial_soc_kwh, 60.0)

    def test_soc_within_range_does_not_log(self):
        data = dict(_TESLA_DATA)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        # nimbus issue #757 (temporary): same assertLogs-with-filter
        # adjustment as TestSocExcursionWarnOnce's own
        # test_unavailable_participant_never_warns_even_while_outside_
        # floor -- see that test's own comment. Revert once #757 is
        # root-caused and the diagnostic lines are removed.
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as cm:
            solver_writer.build_extra_batteries()
        unexpected = [line for line in cm.output if "Nimbus #757 diag" not in line]
        self.assertEqual(unexpected, [])


class TestSocExcursionWarnOnce(unittest.TestCase):
    """nimbus issue #601 (Mark Purcell, real finding: 35 WARNING lines in
    51 minutes for one parked EV recovering slowly on solar): the SoC-
    outside-floor/ceiling warning fires WARNING once per excursion, DEBUG
    on every cycle it stays outside, one INFO on recovery, and is skipped
    entirely while the participant is unavailable."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        solver_writer._BATTERY_PARTICIPANT_WARNED.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        solver_writer._BATTERY_PARTICIPANT_WARNED.clear()

    def _below_floor_hass(self):
        data = dict(_TESLA_DATA)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("16.11")},
        )

    def test_first_excursion_cycle_logs_a_real_warning(self):
        self._below_floor_hass()
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as cm:
            solver_writer.build_extra_batteries()
        self.assertTrue(
            any("ev_m3p" in line and "outside" in line for line in cm.output)
        )

    def test_second_consecutive_excursion_cycle_is_debug_not_warning(self):
        self._below_floor_hass()
        solver_writer.build_extra_batteries()  # first cycle: WARNING, consumes the key
        with (
            self.assertNoLogs(solver_writer._LOGGER, level="WARNING"),
            self.assertLogs(solver_writer._LOGGER, level="DEBUG") as cm,
        ):
            solver_writer.build_extra_batteries()
        self.assertTrue(
            any("ev_m3p" in line and "still outside" in line for line in cm.output)
        )

    def test_recovery_cycle_logs_one_info_and_resets_the_key(self):
        self._below_floor_hass()
        solver_writer.build_extra_batteries()  # establishes the excursion
        data = dict(_TESLA_DATA)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},  # back inside
        )
        with self.assertLogs(solver_writer._LOGGER, level="INFO") as cm:
            solver_writer.build_extra_batteries()
        self.assertTrue(any("recovered" in line for line in cm.output))
        self.assertNotIn(
            "ev_m3p:soc_excursion", solver_writer._BATTERY_PARTICIPANT_WARNED
        )

        # A subsequent excursion after recovering must warn again (not
        # stay silently suppressed by a stale key).
        self._below_floor_hass()
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as cm2:
            solver_writer.build_extra_batteries()
        self.assertTrue(any("outside" in line for line in cm2.output))

    def test_unavailable_participant_never_warns_even_while_outside_floor(self):
        # Below floor (16.11% < 20% configured min) AND gated unavailable
        # -- the LP cannot recover it and the household cannot act on it,
        # so nothing should be logged at all.
        data = dict(_TESLA_DATA)
        data["battery_participant_available_entity"] = "binary_sensor.away"
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={
                "sensor.m3p_t_battery_level": _fake_state("16.11"),
                "binary_sensor.away": _fake_state("off"),
            },
        )
        # nimbus issue #757 (temporary): build_extra_batteries() carries
        # unconditional diagnostic WARNING logging right now (to find why
        # a live devhub battery_participant subentry wasn't reaching the
        # solve) -- this test's real assertion is "no logging ABOUT this
        # scenario specifically" (no soc-excursion warning for a gated-
        # unavailable participant), not "zero logging at all" now that a
        # deliberate, temporary trace exists on every call. Revert this
        # filter back to assertNoLogs once #757 is root-caused and the
        # diagnostic lines are removed.
        with self.assertLogs(solver_writer._LOGGER, level="DEBUG") as cm:
            solver_writer.build_extra_batteries()
        unexpected = [line for line in cm.output if "Nimbus #757 diag" not in line]
        self.assertEqual(unexpected, [])
        self.assertNotIn(
            "ev_m3p:soc_excursion", solver_writer._BATTERY_PARTICIPANT_WARNED
        )


if __name__ == "__main__":
    unittest.main()
