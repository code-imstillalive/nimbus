"""nimbus issue #768/#585 (Mark Purcell): real tests for
_resolve_battery_participant_history() -- the retrospective, HISTORY-
based sibling of build_extra_batteries() that lets
compute_daily_quality_report() score the whole real battery fleet
(home + EV battery_participants), not just the home pack. Same
solver_writer._NATIVE_HASS-mocking pattern as
test_solver_writer_battery_participants.py.
"""

from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
YESTERDAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
YESTERDAY_END = YESTERDAY_START + timedelta(days=1)


@contextlib.contextmanager
def _patch_history(fetch=None, **kwargs):
    """Back both of this function's history reads with one fixture.

    Since #843 option A, `_resolve_battery_participant_history()` reads a
    participant's POWER sensor through `fetch_entity_power_history_kw()`
    (which preserves per-row `unit_of_measurement`) and everything else
    through the shared, attribute-stripping `fetch_entity_history_range()`.
    Both return `(datetime, kW)` pairs, so a single side_effect fixture
    correctly backs both -- the split is about what the recorder is asked
    for, not about the shape that comes back.
    """
    kw = {"side_effect": fetch} if fetch is not None else kwargs
    with (
        patch.object(solver_writer, "fetch_entity_history_range", **kw),
        patch.object(solver_writer, "fetch_entity_power_history_kw", **kw),
    ):
        yield


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_state(value, entity_id: str = "sensor.fake", attributes: dict | None = None):
    return SimpleNamespace(
        entity_id=entity_id, state=value, attributes=attributes or {}
    )


def _fake_native_hass(subentries: list, states: dict | None = None):
    entry = SimpleNamespace(subentries={s.subentry_id: s for s in subentries})
    resolved_states = {
        eid: SimpleNamespace(entity_id=eid, state=s.state, attributes=s.attributes)
        for eid, s in (states or {}).items()
    }
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        states=SimpleNamespace(get=lambda eid: resolved_states.get(eid)),
    )


def _flat_history(value, day_start, day_end, step_minutes=15):
    out = []
    t = day_start
    while t < day_end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


_EV_DATA = {
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


class TestResolveBatteryParticipantHistory(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._grid_times = [YESTERDAY_START + timedelta(hours=i) for i in range(24)]

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def _call(self):
        return solver_writer._resolve_battery_participant_history(
            day_start=YESTERDAY_START,
            day_end=YESTERDAY_END,
            grid_times=self._grid_times,
            period_hours=1.0,
            n_periods=24,
        )

    def test_returns_empty_list_when_not_in_native_mode(self):
        solver_writer._NATIVE_HASS = None
        self.assertEqual(self._call(), [])

    def test_zero_subentries_is_a_real_no_op(self):
        solver_writer._NATIVE_HASS = _fake_native_hass([])
        self.assertEqual(self._call(), [])

    def test_non_battery_participant_subentries_are_ignored(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "load", {"load_sensor": "sensor.pool"})]
        )
        self.assertEqual(self._call(), [])

    def test_participant_missing_power_sensor_is_skipped_not_crashed(self):
        data = dict(_EV_DATA)
        del data["battery_participant_power_sensor"]
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)]
        )
        self.assertEqual(self._call(), [])

    def test_participant_with_no_real_history_is_skipped_not_crashed(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        with _patch_history(return_value=[]):
            self.assertEqual(self._call(), [])

    def test_builds_a_real_battery_and_dispatch_from_history(self):
        """A steady +5kW reading on a power_positive_is_charge=True
        sensor (SigEnergy-style) must be read as CHARGING throughout --
        same sign convention test_battery_power_sign_convention.py
        already locks in for the home battery, mirrored here for a
        participant."""
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return _flat_history(5.0, YESTERDAY_START, YESTERDAY_END)
            return []

        with _patch_history(fetch):
            results = self._call()
        self.assertEqual(len(results), 1)
        battery, charge_kw, discharge_kw, final_soc_kwh = results[0]
        self.assertEqual(battery.name, "ev_m3p")
        self.assertEqual(battery.capacity_kwh, 60.0)
        self.assertAlmostEqual(battery.initial_soc_kwh, 33.0)  # 55% of 60
        # power_positive_is_charge=True -> a +5kW reading is CHARGING.
        self.assertTrue((charge_kw == 5.0).all())
        self.assertTrue((discharge_kw == 0.0).all())
        self.assertAlmostEqual(final_soc_kwh, 33.0)  # flat SoC history

    def test_participant_config_error_is_skipped_not_crashed(self):
        """A physically-impossible min/max SoC pair (BatteryConfig's own
        __post_init__ rejects max_soc_kwh < min_soc_kwh) must exclude
        just this one participant, never take down the whole day's
        report."""
        bad_data = dict(_EV_DATA)
        bad_data["battery_participant_min_soc_percent"] = 95.0
        bad_data["battery_participant_max_soc_percent"] = 20.0
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", bad_data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
            return []

        with _patch_history(fetch):
            self.assertEqual(self._call(), [])


def _state_history(value, day_start, day_end, step_minutes=15):
    """Same shape as _flat_history() but for a binary_sensor's own raw
    STATE STRING history ("on"/"off"), matching what
    fetch_entity_state_history_range() itself returns."""
    out = []
    t = day_start
    while t < day_end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


class TestAvailabilityGating(unittest.TestCase):
    """nimbus issue #768 (Mark Purcell, 2026-09-13, real catch): the
    pack-power sensor also measures real propulsion discharge WHILE
    DRIVING, which never touches the home's grid connection -- must be
    gated out via battery_participant_available_entity's own HISTORY,
    not left in as if it were a real grid flow."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._grid_times = [YESTERDAY_START + timedelta(hours=i) for i in range(24)]

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def _call(self):
        return solver_writer._resolve_battery_participant_history(
            day_start=YESTERDAY_START,
            day_end=YESTERDAY_END,
            grid_times=self._grid_times,
            period_hours=1.0,
            n_periods=24,
        )

    def test_no_available_entity_is_a_real_no_op(self):
        """Zero available_entity configured (every install before this
        fix) -- gating never activates, byte-identical to before."""
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return _flat_history(5.0, YESTERDAY_START, YESTERDAY_END)
            return []

        with _patch_history(fetch):
            results = self._call()
        self.assertEqual(len(results), 1)
        _, charge_kw, discharge_kw, _ = results[0]
        self.assertTrue((charge_kw == 5.0).all())
        self.assertTrue((discharge_kw == 0.0).all())

    def test_driving_discharge_while_away_is_zeroed(self):
        """Real scenario: car is home charging all day EXCEPT hours
        8-11, when it's away and driving (the pack genuinely discharges
        for propulsion) -- that discharge must never reach the grid
        balance, since it never touched the home's grid connection."""
        data = dict(
            _EV_DATA, battery_participant_available_entity="binary_sensor.m3p_home"
        )
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def net_kw_at(t):
            # Away (driving, discharging at 8kW) for hours 8-11; home and
            # charging at 5kW (raw reading, power_positive_is_charge=True)
            # every other hour.
            if 8 <= t.hour < 11:
                return -8.0
            return 5.0

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return [
                    (
                        YESTERDAY_START + timedelta(hours=h),
                        net_kw_at(YESTERDAY_START + timedelta(hours=h)),
                    )
                    for h in range(24)
                ]
            return []

        def fetch_state(entity_id, start, end):
            if entity_id == "binary_sensor.m3p_home":
                return [
                    (
                        YESTERDAY_START + timedelta(hours=h),
                        "off" if 8 <= h < 11 else "on",
                    )
                    for h in range(24)
                ]
            return []

        with (
            _patch_history(fetch),
            patch.object(
                solver_writer,
                "fetch_entity_state_history_range",
                side_effect=fetch_state,
            ),
        ):
            results = self._call()
        self.assertEqual(len(results), 1)
        _, charge_kw, discharge_kw, _ = results[0]
        # Away hours (8, 9, 10): real driving discharge zeroed on BOTH
        # arrays -- never counted as a home-grid discharge.
        for h in (8, 9, 10):
            self.assertEqual(charge_kw[h], 0.0)
            self.assertEqual(discharge_kw[h], 0.0)
        # Every home hour keeps its real charging flow untouched.
        for h in range(24):
            if h not in (8, 9, 10):
                self.assertEqual(charge_kw[h], 5.0)
                self.assertEqual(discharge_kw[h], 0.0)

    def test_missing_availability_history_defaults_to_away(self):
        """No real history at all for the configured available_entity --
        conservative default is AWAY (0.0), same "if we can't confirm
        the car/resource is really there, don't count it" posture the
        live solve's own gate already uses -- every period is zeroed,
        not silently treated as home."""
        data = dict(
            _EV_DATA, battery_participant_available_entity="binary_sensor.m3p_home"
        )
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", data)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )

        def fetch(entity_id, start, end):
            if entity_id == "sensor.m3p_t_battery_level":
                return _flat_history(55.0, start, end)
            if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
                return _flat_history(5.0, YESTERDAY_START, YESTERDAY_END)
            return []

        with (
            _patch_history(fetch),
            patch.object(
                solver_writer, "fetch_entity_state_history_range", return_value=[]
            ),
        ):
            results = self._call()
        self.assertEqual(len(results), 1)
        _, charge_kw, discharge_kw, _ = results[0]
        self.assertTrue((charge_kw == 0.0).all())
        self.assertTrue((discharge_kw == 0.0).all())


class TestComputeDailyQualityReportIncludesParticipants(unittest.TestCase):
    """End-to-end: compute_daily_quality_report() with a real EV
    battery_participant configured alongside the home battery genuinely
    scores both, jointly -- nimbus #768's own production-wiring ask."""

    def _cfg(self, **overrides):
        cfg = {
            "solver_solar_power_sensor": "sensor.real_solar",
            "solver_battery_power_sensor": "sensor.real_battery",
            "solver_whole_house_cross_check_sensor": "sensor.real_load",
            "solver_import_price_sensor": "sensor.import_price",
            "solver_export_price_sensor": "sensor.export_price",
            "solver_battery_capacity_kwh": 50.0,
            "solver_battery_min_soc_percent": 5.0,
            "solver_battery_max_soc_percent": 100.0,
            "solver_max_charge_kw": 10.0,
            "solver_max_discharge_kw": 10.0,
            "solver_efficiency_percent": 95.0,
            "solver_charge_cost": 0.01,
            "solver_discharge_cost": 0.01,
            "solver_salvage_value": 0.1,
            "solver_grid_max_import_kw": 20.0,
            "solver_grid_max_export_kw": 20.0,
        }
        cfg.update(overrides)
        return cfg

    def _fetch(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _flat_history(0.20, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.export_price":
            return _flat_history(0.05, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.m3p_t_battery_level":
            return _flat_history(55.0, start, end)
        if entity_id == "sensor.sigen_inverter_dc_charger_output_power":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        return []

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_ev_participant_is_included_in_scored_participants(self):
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "battery_participant", _EV_DATA)],
            states={"sensor.m3p_t_battery_level": _fake_state("55.0")},
        )
        with _patch_history(self._fetch):
            report = solver_writer.compute_daily_quality_report(self._cfg(), NOW)
        self.assertIsNotNone(report)
        self.assertEqual(report["scored_participants"], ["home", "ev_m3p"])
        # Both batteries idle all day (flat SoC, zero net power) -- same
        # exact-equality structural property TestComputeDailyQualityReport
        # RealScore's own single-battery test relies on, now true of the
        # whole fleet's own aggregate j_ach vs j_ref.
        self.assertAlmostEqual(report["j_ach"], report["j_ref"], places=6)

    def test_no_participant_subentries_still_scores_home_only(self):
        """Zero battery_participant subentries (every install before
        #768/#585, and any install with none configured) is a real
        no-op -- scored_participants stays exactly ["home"]."""
        solver_writer._NATIVE_HASS = _fake_native_hass([])
        with _patch_history(self._fetch):
            report = solver_writer.compute_daily_quality_report(self._cfg(), NOW)
        self.assertIsNotNone(report)
        self.assertEqual(report["scored_participants"], ["home"])


if __name__ == "__main__":
    unittest.main()
