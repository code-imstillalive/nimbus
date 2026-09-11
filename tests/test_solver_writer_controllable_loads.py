"""nimbus issue #486: real tests for _resolve_hour_to_period_index() and
build_controllable_loads() -- solver_writer.py's own real controllable-
load subentry -> LP config wiring, replacing the two hardcoded empty
lists network.build_plan() used to always be called with.

Uses solver_writer._NATIVE_HASS directly (module-level global, reset
after each test) rather than the full HA test harness -- these two
functions are pure Python beyond that one seam (config_entries.
async_entries()), and mocking it directly is the same pattern already
established by this project's own test_solver_writer_* files for
similar module-level-state functions.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import _solver_path  # noqa: F401
import load_run_state
import solver_writer
import thermal_forecast

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


def _grid(start: datetime, n: int, minutes: int = 30) -> list[datetime]:
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


class TestResolveHourToPeriodIndex(unittest.TestCase):
    def test_earliest_hour_still_ahead_today_resolves_to_todays_occurrence(self):
        now = datetime(2026, 9, 7, 3, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48)  # 24h of 30-min periods from 03:00
        # 6.0 (6am) is still ahead of 03:00 today -- period index 6 is
        # 03:00 + 6*30min = 06:00.
        idx = solver_writer._resolve_hour_to_period_index(
            grid_times, now, 6.0, is_deadline=False
        )
        self.assertEqual(grid_times[idx], datetime(2026, 9, 7, 6, 0, tzinfo=_TZ))

    def test_earliest_hour_already_passed_today_resolves_to_tomorrow(self):
        now = datetime(2026, 9, 7, 10, 0, tzinfo=_TZ)
        grid_times = _grid(now, 96)  # 48h of 30-min periods from 10:00
        # 6.0 (6am) already passed today at 10:00 -- must resolve to
        # 6am TOMORROW, not a nonsensical negative/past offset.
        idx = solver_writer._resolve_hour_to_period_index(
            grid_times, now, 6.0, is_deadline=False
        )
        self.assertEqual(grid_times[idx], datetime(2026, 9, 8, 6, 0, tzinfo=_TZ))

    def test_deadline_resolves_to_the_last_period_at_or_before_the_target(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)
        # Deadline 6.0 (6am) -- period 12 is exactly 06:00 (0 + 12*30min).
        # "Inclusive" deadline means period 12 itself, not 11 or 13.
        idx = solver_writer._resolve_hour_to_period_index(
            grid_times, now, 6.0, is_deadline=True
        )
        self.assertEqual(grid_times[idx], datetime(2026, 9, 7, 6, 0, tzinfo=_TZ))

    def test_deadline_between_two_periods_picks_the_earlier_one(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)
        # 6.25 (6:15am) falls strictly between period 12 (06:00) and 13
        # (06:30) -- the deadline constraint needs cumulative energy
        # through a real period whose start is <= the target, so period
        # 12 is correct (period 13 hasn't even started at 6:15).
        idx = solver_writer._resolve_hour_to_period_index(
            grid_times, now, 6.25, is_deadline=True
        )
        self.assertEqual(grid_times[idx], datetime(2026, 9, 7, 6, 0, tzinfo=_TZ))

    def test_target_beyond_the_grids_own_horizon_clamps_to_the_last_period(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=30)  # only 2h of real grid
        idx = solver_writer._resolve_hour_to_period_index(
            grid_times, now, 12.0, is_deadline=False
        )
        self.assertEqual(idx, len(grid_times) - 1)


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_native_hass(
    subentries: list, states: dict | None = None, entry_id: str = "entry_1", loop=None
):
    entry = SimpleNamespace(
        entry_id=entry_id, subentries={s.subentry_id: s for s in subentries}
    )
    fake = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry])
    )
    if states is not None:
        fake.states = SimpleNamespace(get=lambda eid: states.get(eid))
    if loop is not None:
        fake.loop = loop
    return fake


def _fake_state(value, unit=None):
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit})


def _fake_water_heater_state(
    mode="eco", current_temperature=None, temperature=None, max_temp=None, min_temp=None
):
    attrs = {}
    if current_temperature is not None:
        attrs["current_temperature"] = current_temperature
    if temperature is not None:
        attrs["temperature"] = temperature
    if max_temp is not None:
        attrs["max_temp"] = max_temp
    if min_temp is not None:
        attrs["min_temp"] = min_temp
    return SimpleNamespace(state=mode, attributes=attrs)


class TestBuildControllableLoads(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_returns_empty_lists_when_not_in_native_mode(self):
        solver_writer._NATIVE_HASS = None
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, _grid(now, 4), 4
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(adequacy, [])

    def test_builds_a_real_sheddable_load_from_its_subentry(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Pool Pump",
                        "controllable_load_kind": "sheddable",
                        "sheddable_nominal_kw": 1.5,
                        "sheddable_min_fraction": 0.2,
                        "sheddable_shed_cost": 3.0,
                    },
                )
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(adequacy, [])
        self.assertEqual(len(sheddable), 1)
        load = sheddable[0]
        self.assertEqual(load.name, "Pool Pump")
        self.assertEqual(load.shed_cost, 3.0)
        self.assertEqual(load.min_fraction, 0.2)
        self.assertTrue((load.forecast_kw == 1.5).all())
        self.assertEqual(len(load.forecast_kw), len(grid_times))

    def test_builds_a_real_deferrable_load_with_resolved_period_indices(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)  # 24h @ 30min
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_earliest_hour": 1.0,
                        "deferrable_deadline_hour": 6.0,
                        "deferrable_shortfall_price": 8.0,
                    },
                )
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(len(adequacy), 1)
        load = adequacy[0]
        self.assertEqual(load.name, "HWS L1")
        self.assertEqual(load.max_power_kw, 3.7)
        self.assertEqual(load.target_kwh, 5.0)
        self.assertEqual(load.shortfall_price, 8.0)
        self.assertIsNone(load.value_per_kwh)
        # 1.0 (1am) -> period index 2 (00:00 + 2*30min); 6.0 (6am) ->
        # period index 12 (00:00 + 12*30min).
        self.assertEqual(load.earliest_period, 2)
        self.assertEqual(load.deadline_period, 12)
        # nimbus issue #612: earliest/deadline hours both set (same-day
        # shape) -- this load now goes through the recurring-window
        # path. Only one real window fits in this 24h-only synthetic
        # grid (day+1's own 1am earliest is beyond it), but `windows`
        # must still be populated (not None) rather than silently
        # falling back to the pre-#612 single-window shape.
        self.assertIsNotNone(load.windows)
        self.assertEqual(len(load.windows), 1)
        self.assertEqual(load.windows[0].earliest_period, 2)
        self.assertEqual(load.windows[0].deadline_period, 12)
        self.assertEqual(load.windows[0].target_kwh, 5.0)

    def test_deferrable_load_is_scheduled_when_now_is_inside_its_daytime_window(self):
        # nimbus issue #582 (Mark Purcell, first live morning of #534):
        # exact real repro -- earliest=6, deadline=16, now=06:01, on a
        # REAL build_tiered_grid() per the issue's own suggested test.
        # Before the fix, earliest_period resolved to TOMORROW 06:00
        # (the next occurrence of 6am strictly after 06:01) while
        # deadline_period stayed at TODAY 16:00, so deadline_period <
        # earliest_period and the load was skipped for the entire
        # window it was supposed to be active in.
        now = datetime(2026, 9, 9, 6, 1, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Hot Water Heat Pump",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 2.0,
                        "deferrable_earliest_hour": 6.0,
                        "deferrable_deadline_hour": 16.0,
                    },
                )
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(len(adequacy), 1, "load must not be skipped this cycle")
        load = adequacy[0]
        self.assertEqual(load.name, "Hot Water Heat Pump")
        # Window is already open -- earliest is "right now" (period 0),
        # not tomorrow.
        self.assertEqual(load.earliest_period, 0)
        # Deadline still resolves to today's 16:00, completely unaffected.
        deadline_target = datetime(2026, 9, 9, 16, 0, tzinfo=_TZ)
        self.assertLessEqual(grid_times[load.deadline_period], deadline_target)
        self.assertGreater(
            grid_times[min(load.deadline_period + 1, len(grid_times) - 1)],
            deadline_target,
        )

    def test_deferrable_daytime_window_resolves_normally_before_it_opens(self):
        # Same-day window, but `now` is well before it opens -- confirms
        # the #582 fix doesn't change the already-correct ahead-of-window
        # case (earliest resolves to later today, deadline also today,
        # in the natural order -- no skip should ever have been at risk
        # here, this just guards the fix didn't introduce one).
        now = datetime(2026, 9, 9, 3, 0, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Hot Water Heat Pump",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 2.0,
                        "deferrable_earliest_hour": 6.0,
                        "deferrable_deadline_hour": 16.0,
                    },
                )
            ]
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1)
        # earliest_period is real today-06:00, not the "right now" (0)
        # override -- the window hasn't opened yet, so no override should
        # fire.
        self.assertGreater(adequacy[0].earliest_period, 0)
        earliest_target = datetime(2026, 9, 9, 6, 0, tzinfo=_TZ)
        self.assertLessEqual(grid_times[adequacy[0].earliest_period], earliest_target)
        # nimbus issue #612: a real 96h tiered grid fits multiple days of
        # this same-day-shaped window -- windows must be populated with
        # more than just today's.
        self.assertIsNotNone(adequacy[0].windows)
        self.assertGreaterEqual(len(adequacy[0].windows), 3)

    def test_overnight_window_still_resolves_normally_before_it_opens(self):
        # The genuine overnight case (earliest=22, deadline=6) the
        # original skip branch's own comment describes, at a `now` well
        # before tonight's window opens -- must keep working exactly as
        # before the #582 fix (deadline correctly rolls to tomorrow,
        # ahead of tonight's still-pending earliest).
        now = datetime(2026, 9, 9, 10, 0, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Overnight EV Charge",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 7.0,
                        "deferrable_target_kwh": 10.0,
                        "deferrable_earliest_hour": 22.0,
                        "deferrable_deadline_hour": 6.0,
                    },
                )
            ]
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1, "genuine overnight case must still resolve")
        self.assertLess(adequacy[0].earliest_period, adequacy[0].deadline_period)
        # nimbus issue #612: an overnight window (deadline < earliest)
        # falls through to the pre-#612 single-window path unchanged --
        # windows must stay None, not attempt the same-day recurring
        # treatment on a shape it was never validated against.
        self.assertIsNone(adequacy[0].windows)

    def test_deferrable_value_per_kwh_carried_through_when_set(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Miner",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 5.0,
                        "deferrable_target_kwh": 0.5,
                        "deferrable_value_per_kwh": 0.10,
                    },
                )
            ]
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(adequacy[0].value_per_kwh, 0.10)

    def test_sheddable_missing_nominal_kw_is_skipped_not_crashed(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Broken",
                        "controllable_load_kind": "sheddable",
                    },
                )
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(adequacy, [])

    def test_deferrable_missing_target_kwh_is_skipped_not_crashed(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Broken",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                    },
                )
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(adequacy, [])

    def test_deferrable_load_missing_deadline_hour_stays_single_window(self):
        # nimbus issue #612: only earliest_hour set, no deadline_hour --
        # no real day-boundary shape to repeat, must fall through to the
        # pre-#612 single-window path (deadline_period defaults to the
        # last period of the whole horizon) rather than attempting a
        # recurring-window treatment with an undefined deadline.
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "No Deadline",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_earliest_hour": 1.0,
                    },
                )
            ]
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1)
        self.assertIsNone(adequacy[0].windows)
        self.assertEqual(adequacy[0].deadline_period, len(grid_times) - 1)

    def test_non_controllable_load_subentries_are_ignored(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [_fake_subentry("s1", "load", {"load_sensor": "sensor.pool"})]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(adequacy, [])

    def test_multiple_subentries_of_both_kinds_all_get_built(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Pool Pump",
                        "controllable_load_kind": "sheddable",
                        "sheddable_nominal_kw": 1.5,
                    },
                ),
                _fake_subentry(
                    "s2",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                    },
                ),
                _fake_subentry(
                    "s3",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L3",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                    },
                ),
            ]
        )
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(sheddable), 1)
        self.assertEqual(len(adequacy), 2)
        self.assertEqual({a.name for a in adequacy}, {"HWS L1", "HWS L3"})


class TestParseDoneWhen(unittest.TestCase):
    """nimbus issue #480: _parse_done_when()'s own small, fixed
    comparison DSL -- deliberately not eval()."""

    def test_ge(self):
        op_fn, threshold = solver_writer._parse_done_when(">= 60")
        self.assertTrue(op_fn(60.0, threshold))
        self.assertFalse(op_fn(59.9, threshold))

    def test_le(self):
        op_fn, threshold = solver_writer._parse_done_when("<=20")
        self.assertTrue(op_fn(20.0, threshold))
        self.assertFalse(op_fn(20.1, threshold))

    def test_longer_operators_checked_before_shorter_ones(self):
        # ">= 60" must not be misparsed as "> = 60" (which would fail to
        # float-parse " = 60" and raise).
        _, threshold = solver_writer._parse_done_when(">= 60")
        self.assertEqual(threshold, 60.0)

    def test_eq_and_ne(self):
        op_fn, threshold = solver_writer._parse_done_when("==1")
        self.assertTrue(op_fn(1.0, threshold))
        op_fn, threshold = solver_writer._parse_done_when("!= 0")
        self.assertTrue(op_fn(1.0, threshold))
        self.assertFalse(op_fn(0.0, threshold))

    def test_no_recognized_operator_raises_value_error(self):
        with self.assertRaises(ValueError):
            solver_writer._parse_done_when("60")

    def test_non_numeric_threshold_raises_value_error(self):
        with self.assertRaises(ValueError):
            solver_writer._parse_done_when(">= hot")


class TestEvaluateDoneCondition(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._orig_warned = set(solver_writer._DONE_CONDITION_WARNED)
        solver_writer._DONE_CONDITION_WARNED.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        solver_writer._DONE_CONDITION_WARNED.clear()
        solver_writer._DONE_CONDITION_WARNED.update(self._orig_warned)

    def test_no_native_hass_returns_none(self):
        solver_writer._NATIVE_HASS = None
        result = solver_writer._evaluate_done_condition("binary_sensor.x", None)
        self.assertIsNone(result)

    def test_missing_entity_returns_none(self):
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: None)
        )
        result = solver_writer._evaluate_done_condition("binary_sensor.x", None)
        self.assertIsNone(result)

    def test_unavailable_entity_returns_none(self):
        states = {"binary_sensor.x": _fake_state("unavailable")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        result = solver_writer._evaluate_done_condition("binary_sensor.x", None)
        self.assertIsNone(result)

    def test_binary_sensor_on_with_no_done_when_is_done(self):
        states = {"binary_sensor.x": _fake_state("on")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        self.assertTrue(solver_writer._evaluate_done_condition("binary_sensor.x", None))

    def test_binary_sensor_off_with_no_done_when_is_not_done(self):
        states = {"binary_sensor.x": _fake_state("off")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        self.assertFalse(
            solver_writer._evaluate_done_condition("binary_sensor.x", None)
        )

    def test_numeric_sensor_with_done_when_met_is_done(self):
        states = {"sensor.tank_temp": _fake_state("62.5")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        self.assertTrue(
            solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")
        )

    def test_numeric_sensor_with_done_when_not_met_is_not_done(self):
        states = {"sensor.tank_temp": _fake_state("45.0")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        self.assertFalse(
            solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")
        )

    def test_malformed_done_when_returns_none_not_raise(self):
        states = {"sensor.tank_temp": _fake_state("62.5")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        result = solver_writer._evaluate_done_condition("sensor.tank_temp", "hot")
        self.assertIsNone(result)

    def test_non_numeric_state_with_done_when_returns_none_not_raise(self):
        states = {"sensor.tank_temp": _fake_state("not_a_number")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        result = solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")
        self.assertIsNone(result)

    def test_the_same_bad_condition_only_warns_once(self):
        # nimbus issue #480, Mark Purcell's own live review: this used
        # to warn on every single solve tick (~5 min) for as long as the
        # same bad condition persisted -- the #313/#314 "log once per
        # condition" discipline this project already follows elsewhere.
        states = {"sensor.tank_temp": _fake_state("not_a_number")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")
        self.assertEqual(len(captured.records), 1)
        # Second call, same exact (entity, done_when, state) triple --
        # must NOT log again. assertNoLogs would raise AssertionError on
        # zero records, which is exactly what "no second warning" means.
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="WARNING"),
        ):
            solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")

    def test_a_genuinely_different_bad_state_warns_again(self):
        # A DIFFERENT bad reading on the same entity/done_when is a real,
        # new diagnostic event -- must still get its own one-time log,
        # not be suppressed by the earlier condition's own dedup key.
        states = {"sensor.tank_temp": _fake_state("still_not_a_number")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid))
        )
        solver_writer._DONE_CONDITION_WARNED.add(
            ("sensor.tank_temp", ">= 60", "not_a_number")
        )
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            solver_writer._evaluate_done_condition("sensor.tank_temp", ">= 60")
        self.assertEqual(len(captured.records), 1)


class TestEvaluateDoneConditionAttributeDomains(unittest.TestCase):
    """nimbus issue #534 (Mark Purcell, real SG-Ready heat-pump HWS
    install): water_heater/climate's own state is a mode string ("eco"),
    not a number -- done_when has to read current_temperature (an
    attribute) instead, and an unset done_when falls back to the
    entity's own temperature (setpoint) attribute rather than the
    binary_sensor "state == on" default every other domain uses."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._orig_warned = set(solver_writer._DONE_CONDITION_WARNED)
        solver_writer._DONE_CONDITION_WARNED.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        solver_writer._DONE_CONDITION_WARNED.clear()
        solver_writer._DONE_CONDITION_WARNED.update(self._orig_warned)

    def _hass(self, entity_id, state_obj):
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: {entity_id: state_obj}.get(eid))
        )

    def test_no_done_when_defaults_to_current_temperature_ge_temperature_attr(self):
        # #534's own real 7 Sep case: tank at 61C, setpoint (temperature
        # attribute) 45C in eco -- already past setpoint, so done.
        self._hass(
            "water_heater.hws",
            _fake_water_heater_state(current_temperature=61.0, temperature=45.0),
        )
        self.assertTrue(
            solver_writer._evaluate_done_condition("water_heater.hws", None)
        )

    def test_no_done_when_below_temperature_attr_is_not_done(self):
        self._hass(
            "water_heater.hws",
            _fake_water_heater_state(current_temperature=48.0, temperature=65.0),
        )
        self.assertFalse(
            solver_writer._evaluate_done_condition("water_heater.hws", None)
        )

    def test_explicit_done_when_reads_current_temperature_not_the_mode_state(self):
        # state is "eco" (not numeric) -- must not be parsed as the
        # comparison value; current_temperature attribute is used instead.
        self._hass(
            "water_heater.hws",
            _fake_water_heater_state(mode="eco", current_temperature=62.5),
        )
        self.assertTrue(
            solver_writer._evaluate_done_condition("water_heater.hws", ">= 60")
        )

    def test_climate_domain_gets_the_same_attribute_based_evaluation(self):
        self._hass(
            "climate.zone1",
            _fake_water_heater_state(mode="heat", current_temperature=22.0),
        )
        self.assertTrue(
            solver_writer._evaluate_done_condition("climate.zone1", ">= 21")
        )

    def test_missing_current_temperature_attribute_returns_none(self):
        self._hass("water_heater.hws", _fake_water_heater_state(mode="eco"))
        result = solver_writer._evaluate_done_condition("water_heater.hws", None)
        self.assertIsNone(result)

    def test_no_done_when_and_missing_temperature_attr_returns_none(self):
        self._hass(
            "water_heater.hws",
            _fake_water_heater_state(current_temperature=55.0),
        )
        result = solver_writer._evaluate_done_condition("water_heater.hws", None)
        self.assertIsNone(result)

    def test_unavailable_water_heater_fails_open_returns_none(self):
        # Same #480 fail-open contract -- #534's own real install
        # republishes MQTT availability roughly hourly, blipping every
        # entity through unavailable/unknown; must never be read as
        # "not done, restart the schedule".
        self._hass("water_heater.hws", _fake_state("unavailable"))
        result = solver_writer._evaluate_done_condition("water_heater.hws", None)
        self.assertIsNone(result)

    def test_malformed_done_when_on_a_water_heater_warns_once_not_every_cycle(self):
        self._hass(
            "water_heater.hws",
            _fake_water_heater_state(current_temperature=62.5),
        )
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            solver_writer._evaluate_done_condition("water_heater.hws", "hot")
        self.assertEqual(len(captured.records), 1)
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="WARNING"),
        ):
            solver_writer._evaluate_done_condition("water_heater.hws", "hot")


class TestBuildDailyAdequacyWindows(unittest.TestCase):
    """nimbus issue #612: direct tests for _build_daily_adequacy_windows(),
    the helper that gives a same-day-shaped deferrable load a fresh
    target_kwh every calendar day within the horizon instead of once."""

    def test_a_96h_horizon_produces_one_window_per_day(self):
        now = datetime(2026, 9, 9, 3, 0, tzinfo=_TZ)  # before today's window opens
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times,
            now,
            6.0,
            16.0,
            2.0,
            today_delivered_kwh=0.0,
            today_done=False,
        )
        # A 96h horizon starting at 03:00 covers today's window fully,
        # plus 3 more full days -- 4 windows total.
        self.assertEqual(len(windows), 4)
        for w in windows:
            self.assertEqual(w.target_kwh, 2.0)
            self.assertGreaterEqual(w.deadline_period, w.earliest_period)

    def test_todays_window_still_open_resolves_to_right_now(self):
        # Same real repro as #582's own test: earliest=6, deadline=16,
        # now=06:01 -- today's own window must start at period 0.
        now = datetime(2026, 9, 9, 6, 1, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=0.0, today_done=False
        )
        self.assertEqual(windows[0].earliest_period, 0)

    def test_todays_window_already_closed_skips_to_tomorrow(self):
        now = datetime(
            2026, 9, 9, 20, 0, tzinfo=_TZ
        )  # well past today's 16:00 deadline
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=0.0, today_done=False
        )
        # First real window is TOMORROW's, not today's (which is gone).
        first_window_start = grid_times[windows[0].earliest_period]
        self.assertEqual(
            first_window_start.date(), datetime(2026, 9, 10, tzinfo=_TZ).date()
        )

    def test_todays_delivered_energy_reduces_only_todays_window(self):
        now = datetime(2026, 9, 9, 13, 5, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=1.48, today_done=False
        )
        self.assertAlmostEqual(windows[0].target_kwh, 0.52, places=6)
        # Every future day keeps the FULL, unreduced target.
        for w in windows[1:]:
            self.assertEqual(w.target_kwh, 2.0)

    def test_todays_target_fully_met_drops_only_todays_window(self):
        now = datetime(2026, 9, 9, 13, 5, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=2.5, today_done=False
        )
        # No window starts on today's own date -- every one is a real
        # future day (a 96h horizon from 13:05 reaches far enough into
        # day+4 for its own 06:00 earliest to still qualify, even though
        # its own 16:00 deadline gets clamped to the horizon's end).
        self.assertGreaterEqual(len(windows), 3)
        for w in windows:
            self.assertEqual(w.target_kwh, 2.0)
            self.assertGreater(grid_times[w.earliest_period].date(), now.date())

    def test_today_done_drops_only_todays_window(self):
        now = datetime(2026, 9, 9, 13, 5, tzinfo=_TZ)
        grid_times, _ = solver_writer.build_tiered_grid(now)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=0.0, today_done=True
        )
        self.assertGreaterEqual(len(windows), 3)
        for w in windows:
            self.assertGreater(grid_times[w.earliest_period].date(), now.date())

    def test_last_window_partially_beyond_the_horizon_is_clamped_not_dropped(self):
        now = datetime(2026, 9, 9, 3, 0, tzinfo=_TZ)
        # A short 7h horizon: today's 06:00 earliest fits, but its own
        # 16:00 deadline runs well past the grid's own last period --
        # must clamp to the last real period, not drop the window.
        grid_times = _grid(now, 8, minutes=60)
        windows = solver_writer._build_daily_adequacy_windows(
            grid_times, now, 6.0, 16.0, 2.0, today_delivered_kwh=0.0, today_done=False
        )
        # Only today's window fits at all (a second day's own 06:00
        # earliest, tomorrow, is well beyond this short horizon).
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].deadline_period, len(grid_times) - 1)


class TestBuildControllableLoadsEarlyCompletion(unittest.TestCase):
    """nimbus issue #480: real tests for build_controllable_loads()'s own
    done_entity wiring on deferrable loads."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def test_a_done_binary_sensor_releases_the_load_this_cycle(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_done_entity": "binary_sensor.hws_at_temp",
                    },
                )
            ],
            states={"binary_sensor.hws_at_temp": _fake_state("on")},
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(adequacy, [])

    def test_a_not_yet_done_binary_sensor_schedules_normally(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_done_entity": "binary_sensor.hws_at_temp",
                    },
                )
            ],
            states={"binary_sensor.hws_at_temp": _fake_state("off")},
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1)

    def test_an_unavailable_done_entity_fails_open_and_schedules_normally(self):
        # #480's own acceptance criterion: "a done-sensor going
        # unavailable is ignored (fail open: keep the schedule)."
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_done_entity": "binary_sensor.hws_at_temp",
                    },
                )
            ],
            states={"binary_sensor.hws_at_temp": _fake_state("unavailable")},
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1)

    def test_a_numeric_done_sensor_with_done_when_releases_the_load(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                        "deferrable_done_entity": "sensor.tank_temp",
                        "deferrable_done_when": ">= 60",
                    },
                )
            ],
            states={"sensor.tank_temp": _fake_state("65.0")},
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(adequacy, [])

    def test_no_done_entity_configured_is_unaffected(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 8)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 3.7,
                        "deferrable_target_kwh": 5.0,
                    },
                )
            ]
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(len(adequacy), 1)


class TestBuildControllableLoadsFloorCrossing(unittest.TestCase):
    """nimbus issue #712/#713 (Mark Purcell, real live finding: two
    consecutive nights of uncontrolled compressor cut-in on the WWK302/
    #534 heat pump). build_controllable_loads()'s own real fix: when a
    naive, plan-free decay-only projection shows the tank crossing its
    physical floor before the nearest window's own deadline, that
    window's deadline is tightened to force real delivery before the
    breach -- not merely widening the earliest bound, which #713's own
    real numbers (earliest 06:00, crossing 07:30, chosen start 08:00)
    show would NOT have changed anything, since the LP was already free
    to start at 06:00 and simply preferred the cheaper 08:00."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass

    def _build(
        self, *, current_temperature, min_temp, earliest_hour=6.0, deadline_hour=16.0
    ):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)  # 24h @ 30min
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 4.0,
                        "deferrable_earliest_hour": earliest_hour,
                        "deferrable_deadline_hour": deadline_hour,
                        "deferrable_done_entity": "water_heater.wwk302",
                    },
                )
            ],
            states={
                "water_heater.wwk302": _fake_water_heater_state(
                    current_temperature=current_temperature, min_temp=min_temp
                )
            },
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        return adequacy

    def test_imminent_crossing_tightens_the_nearest_windows_deadline(self):
        # #712's own real numbers: tank at 50 degC, eco floor 45 degC --
        # DEFAULT_IDLE_DECAY_C_PER_HOUR (0.5 degC/h) crosses in 10h, i.e.
        # 10:00 (period 20 on a 30-min grid from midnight). Original
        # window: earliest 06:00 (period 12), deadline 16:00 (period 32).
        adequacy = self._build(current_temperature=50.0, min_temp=45.0)
        self.assertEqual(len(adequacy), 1)
        windows = adequacy[0].windows
        self.assertIsNotNone(windows)
        self.assertEqual(windows[0].earliest_period, 12)
        self.assertEqual(windows[0].deadline_period, 20)
        # Target/earliest untouched -- only the deadline tightens.
        self.assertEqual(windows[0].target_kwh, 4.0)

    def test_no_crossing_before_the_original_deadline_leaves_it_untouched(self):
        # A high floor-to-current gap (55 degC of margin at 0.5 degC/h =
        # 110h) never crosses within this 24h grid at all -- deadline
        # stays exactly what _build_daily_adequacy_windows() computed.
        adequacy = self._build(current_temperature=50.0, min_temp=-5.0)
        windows = adequacy[0].windows
        self.assertEqual(windows[0].deadline_period, 32)

    def test_crossing_already_past_earliest_clamps_to_earliest_not_inverted(self):
        # An imminent crossing (30 min away) that lands BEFORE the
        # window's own earliest_period (06:00) must never invert the
        # window (deadline < earliest) -- clamps to earliest_period
        # itself, the earliest moment the load could possibly react.
        adequacy = self._build(current_temperature=45.25, min_temp=45.0)
        windows = adequacy[0].windows
        self.assertEqual(windows[0].earliest_period, 12)
        self.assertEqual(windows[0].deadline_period, 12)

    def test_tank_already_at_or_below_floor_is_a_no_op_here(self):
        # Already-crossed is a live-dispatch/done-condition concern (the
        # device is presumably already self-heating right now), not a
        # scheduling one -- naive_floor_crossing_period() returns None
        # for this case by design, so the window is left exactly as
        # _build_daily_adequacy_windows() computed it.
        adequacy = self._build(current_temperature=44.0, min_temp=45.0)
        windows = adequacy[0].windows
        self.assertEqual(windows[0].deadline_period, 32)

    def test_non_water_heater_done_entity_is_unaffected(self):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "HWS L1",
                        "controllable_load_kind": "deferrable",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 4.0,
                        "deferrable_earliest_hour": 6.0,
                        "deferrable_deadline_hour": 16.0,
                        "deferrable_done_entity": "sensor.tank_temp",
                        "deferrable_done_when": ">= 60",
                    },
                )
            ],
            states={"sensor.tank_temp": _fake_state("50.0")},
        )
        _, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        # A plain numeric sensor isn't in ATTRIBUTE_DONE_DOMAINS --
        # read_current_temperature() only ever reads the
        # current_temperature ATTRIBUTE off a water_heater/climate
        # entity, so this path is a deliberate no-op regardless of the
        # sensor's own state value.
        self.assertEqual(adequacy[0].windows[0].deadline_period, 32)


# nimbus issue #479: tests for _sample_load_run_state() and its wiring
# into build_controllable_loads() -- the one seam solver_writer.py's own
# top-of-file try/except pattern doesn't cover, since this function
# imports `homeassistant.helpers.storage.Store` directly (not through
# this project's own `.const`-style relative/absolute fallback, because
# it's a real HA core module, not one of this package's own). No stub
# exists for it in this bare-module test harness (tests/_ha_stubs.py's
# stub is only ever installed for the full HA-stub suite, which this
# file's own module-level imports never trigger), so a minimal fake is
# injected into sys.modules here, same reasoning tests/_ha_stubs.py's
# own _StubStore gives for number.py's tests.


class _FakeRunStateStore:
    """Real (not mocked) in-memory stand-in for
    homeassistant.helpers.storage.Store, same shape/reasoning as
    tests/_ha_stubs.py's own _StubStore -- keyed by the literal `key`
    string so two instances built with the same key share data."""

    _shared_data: ClassVar[dict] = {}

    def __init__(self, hass, version: int, key: str) -> None:
        self._key = key
        self._shared_data.setdefault(key, None)

    async def async_load(self):
        return self._shared_data.get(self._key)

    async def async_save(self, data) -> None:
        self._shared_data[self._key] = data


_fake_storage_module = types.ModuleType("homeassistant.helpers.storage")
_fake_storage_module.Store = _FakeRunStateStore
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
sys.modules["homeassistant.helpers.storage"] = _fake_storage_module


def _make_running_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """A real, running background-thread event loop -- matches
    _NATIVE_HASS.loop's real shape closely enough for
    asyncio.run_coroutine_threadsafe() (used by _sample_load_run_state,
    same pattern as this file's own fetch_entity_history_range) to
    actually work in a test. Caller must stop+join+close it (see
    TestSampleLoadRunState.tearDown) -- an event loop's own asyncio self-
    pipe holds real OS sockets that only run_forever()'s own thread ever
    releases; leaving it running past the test leaks them as unclosed-
    socket ResourceWarnings, collected by pytest's unraisableexception
    plugin and misattributed to whatever unrelated test happens to be
    running at the next GC pass (confirmed live in CI: exactly this,
    surfacing on test_solver_writer_import_and_token_laziness.py)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


class TestSampleLoadRunState(unittest.TestCase):
    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def test_a_real_sample_is_persisted_to_the_run_state_store(self):
        states = {"sensor.pool_pump_power": _fake_state("1.5")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_1", "s1", "sensor.pool_pump_power", now, "2026-09-07"
        )

        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(None, 1, "nimbus_load_entry_1_load_run_state")
            )
            return await store.async_read("s1")

        result = asyncio.run(_read())
        self.assertTrue(result.currently_on)
        self.assertEqual(result.day_key, "2026-09-07")

    def test_an_unavailable_sensor_is_silently_skipped(self):
        states = {"sensor.pool_pump_power": _fake_state("unavailable")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        # Must not raise -- best-effort bookkeeping, per this function's
        # own docstring.
        solver_writer._sample_load_run_state(
            "entry_1", "s_missing", "sensor.pool_pump_power", now, "2026-09-07"
        )

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def test_a_watts_sensor_is_scaled_down_to_kw(self):
        # nimbus issue #535 (Mark Purcell, real household finding): a
        # 4.6W standby reading on a real heat-pump HWS power sensor used
        # to be read as 4.6 kW -- currently_on permanently true. Same
        # reading, correctly scaled, must read as OFF (4.6W is well
        # under DEFAULT_ON_THRESHOLD_KW=0.05 kW = 50W).
        states = {
            "sensor.hws_power": _fake_state("4.6", unit="W"),
        }
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_w", "s_hws", "sensor.hws_power", now, "2026-09-07"
        )
        result = self._read_state("entry_w", "s_hws")
        self.assertFalse(result.currently_on)

    def test_a_watts_sensor_heating_reading_scales_delivered_kwh_correctly(self):
        # 550W heating for 30 minutes must accrue 0.275 kWh, not 275 kWh
        # (#535's own worked example: "~550 kWh per hour instead of
        # 0.55").
        states = {"sensor.hws_power": _fake_state("550", unit="W")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        t0 = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_w2", "s_hws2", "sensor.hws_power", t0, "2026-09-07"
        )
        t1 = datetime(2026, 9, 7, 8, 30, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_w2", "s_hws2", "sensor.hws_power", t1, "2026-09-07"
        )
        result = self._read_state("entry_w2", "s_hws2")
        self.assertTrue(result.currently_on)
        self.assertAlmostEqual(result.delivered_today_kwh, 0.275, places=3)

    def test_a_kw_sensor_is_unaffected_by_the_scale_check(self):
        # Explicit kW unit -- no scaling, same as the no-unit default.
        states = {"sensor.pool_pump_power": _fake_state("1.5", unit="kW")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_kw", "s_kw", "sensor.pool_pump_power", now, "2026-09-07"
        )
        result = self._read_state("entry_kw", "s_kw")
        self.assertTrue(result.currently_on)

    def test_watt_scaling_hint_logs_once_per_entity(self):
        states = {"sensor.hws_power": _fake_state("550", unit="W")}
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        orig_logged = set(solver_writer._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED)
        solver_writer._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED.clear()
        try:
            now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
            with self.assertLogs(solver_writer._LOGGER, level="INFO") as first:
                solver_writer._sample_load_run_state(
                    "entry_log", "s_log", "sensor.hws_power", now, "2026-09-07"
                )
            self.assertTrue(any("reports Watts" in r.message for r in first.records))
            with (
                self.assertRaises(AssertionError),
                self.assertLogs(solver_writer._LOGGER, level="INFO"),
            ):
                solver_writer._sample_load_run_state(
                    "entry_log",
                    "s_log",
                    "sensor.hws_power",
                    now + timedelta(minutes=5),
                    "2026-09-07",
                )
        finally:
            solver_writer._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED.clear()
            solver_writer._LOAD_POWER_SENSOR_UNIT_HINT_LOGGED.update(orig_logged)

    def test_a_missing_sensor_is_silently_skipped(self):
        solver_writer._NATIVE_HASS = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: None),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        solver_writer._sample_load_run_state(
            "entry_1", "s_missing", "sensor.does_not_exist", now, "2026-09-07"
        )

    def test_build_controllable_loads_samples_run_state_when_power_sensor_configured(
        self,
    ):
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4)
        states = {"sensor.pool_pump_power": _fake_state("2.0")}
        entry = SimpleNamespace(
            entry_id="entry_2",
            subentries={
                "s1": _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Pool Pump",
                        "controllable_load_kind": "sheddable",
                        "sheddable_nominal_kw": 1.5,
                        "controllable_load_power_sensor": "sensor.pool_pump_power",
                    },
                )
            },
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )
        solver_writer.build_controllable_loads(now, grid_times, len(grid_times))

        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(None, 1, "nimbus_load_entry_2_load_run_state")
            )
            return await store.async_read("s1")

        result = asyncio.run(_read())
        self.assertTrue(result.currently_on)

    def _seed_run_state(self, hub_entry_id, subentry_id, state):
        async def _write():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            await store.async_write(subentry_id, state)

        asyncio.run(_write())

    def test_deferrable_target_kwh_is_reduced_by_energy_already_delivered_today(self):
        # nimbus issue #626 (Mark Purcell, real repro): 1.48 of a 2.0 kWh
        # target already delivered at 13:05 -- the LP must only be asked
        # for the REMAINING 0.52 kWh, not the full 2.0 kWh on top of it.
        now = datetime(2026, 9, 7, 13, 5, tzinfo=_TZ)
        day_key = "2026-09-07"
        self._seed_run_state(
            "entry_626",
            "s1",
            load_run_state.LoadRunState(
                delivered_today_kwh=1.48,
                day_key=day_key,
                last_sample_at=now.timestamp(),  # dt_hours=0 this sample
            ),
        )
        states = {"sensor.hws_power": _fake_state("0.65")}
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Hot Water Heat Pump",
                        "controllable_load_kind": "deferrable",
                        "controllable_load_power_sensor": "sensor.hws_power",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 2.0,
                    },
                )
            ],
            states=states,
            entry_id="entry_626",
            loop=self._loop,
        )
        grid_times = _grid(now, 8, minutes=30)
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(len(adequacy), 1)
        self.assertAlmostEqual(adequacy[0].target_kwh, 0.52, places=6)

    def test_deferrable_load_released_entirely_once_todays_target_is_met(self):
        # nimbus issue #626's own follow-up comment: delivered_today_kwh
        # (2.165) already exceeds target_today_kwh (2.0) -- the load must
        # be released for the rest of today's window, not merely reduced
        # to a near-zero-but-still-nonzero remaining target that keeps
        # the LP scheduling it anyway.
        now = datetime(2026, 9, 7, 14, 21, tzinfo=_TZ)
        day_key = "2026-09-07"
        self._seed_run_state(
            "entry_626b",
            "s1",
            load_run_state.LoadRunState(
                delivered_today_kwh=2.165,
                day_key=day_key,
                last_sample_at=now.timestamp(),
            ),
        )
        states = {"sensor.hws_power": _fake_state("0.65")}
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Hot Water Heat Pump",
                        "controllable_load_kind": "deferrable",
                        "controllable_load_power_sensor": "sensor.hws_power",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 2.0,
                    },
                )
            ],
            states=states,
            entry_id="entry_626b",
            loop=self._loop,
        )
        grid_times = _grid(now, 8, minutes=30)
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(adequacy, [])

    def test_deferrable_target_kwh_unaffected_by_a_stale_prior_days_delivery(self):
        # A run-state left over from a PRIOR day (day_key mismatch --
        # apply_power_sample()'s own rollover hasn't been exercised by
        # this fake seed) must never suppress today's real, fresh target.
        now = datetime(2026, 9, 7, 13, 5, tzinfo=_TZ)
        self._seed_run_state(
            "entry_626c",
            "s1",
            load_run_state.LoadRunState(
                delivered_today_kwh=1.9,
                day_key="2026-09-06",
                last_sample_at=(now - timedelta(hours=20)).timestamp(),
            ),
        )
        states = {"sensor.hws_power": _fake_state("0.65")}
        solver_writer._NATIVE_HASS = _fake_native_hass(
            [
                _fake_subentry(
                    "s1",
                    "controllable_load",
                    {
                        "controllable_load_name": "Hot Water Heat Pump",
                        "controllable_load_kind": "deferrable",
                        "controllable_load_power_sensor": "sensor.hws_power",
                        "deferrable_max_power_kw": 0.65,
                        "deferrable_target_kwh": 2.0,
                    },
                )
            ],
            states=states,
            entry_id="entry_626c",
            loop=self._loop,
        )
        grid_times = _grid(now, 8, minutes=30)
        sheddable, adequacy = solver_writer.build_controllable_loads(
            now, grid_times, len(grid_times)
        )
        self.assertEqual(sheddable, [])
        self.assertEqual(len(adequacy), 1)
        # Rolled over to a new day (day_key changes to today as part of
        # this very cycle's own sample) -- today's real target is the
        # full 2.0 kWh, not artificially suppressed by yesterday's number.
        self.assertAlmostEqual(adequacy[0].target_kwh, 2.0, places=6)


def _fake_plan(sheddable=(), adequacy=()):
    return SimpleNamespace(
        sheddable_loads=list(sheddable), adequacy_loads=list(adequacy)
    )


def _fake_load_plan(subentry_id, kw_array, *, adequacy=False, shortfall_kwh=0.0):
    """A stand-in for SheddableLoadPlan/AdequacyLoadPlan -- only the
    fields apply_commanded_state_guard() actually reads."""
    if adequacy:
        return SimpleNamespace(
            subentry_id=subentry_id, power_kw=kw_array, shortfall_kwh=shortfall_kwh
        )
    return SimpleNamespace(subentry_id=subentry_id, served_kw=kw_array)


class TestApplyCommandedStateGuard(unittest.TestCase):
    """nimbus issue #484: real tests for apply_commanded_state_guard()'s
    own wiring -- reading period-0 power off a real Plan-shaped object,
    reaching the same fake Store as TestSampleLoadRunState above (same
    reasoning: no stub exists for homeassistant.helpers.storage in this
    bare-module harness)."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def test_a_sheddable_loads_first_ever_decision_is_persisted_immediately(self):
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [SimpleNamespace(entry_id="entry_a")]
            ),
            loop=self._loop,
        )
        import numpy as np

        plan = _fake_plan(sheddable=[_fake_load_plan("s1", np.array([1.5, 1.5]))])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        result = self._read_state("entry_a", "s1")
        self.assertTrue(result.commanded_state)
        self.assertEqual(result.commanded_since, now.timestamp())

    def test_an_adequacy_loads_period_0_power_below_threshold_commands_off(self):
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [SimpleNamespace(entry_id="entry_b")]
            ),
            loop=self._loop,
        )
        import numpy as np

        plan = _fake_plan(
            adequacy=[_fake_load_plan("s2", np.array([0.0, 3.7]), adequacy=True)]
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        result = self._read_state("entry_b", "s2")
        self.assertFalse(result.commanded_state)

    def test_ten_alternating_solves_produce_at_most_one_real_commanded_change(self):
        # #484's own acceptance criterion, exercised through the real
        # solver_writer.py wiring (not just load_run_state.py's own pure
        # function -- test_load_run_state.py already covers that
        # directly; this confirms the wiring passes the right
        # min_hysteresis_seconds through from the real grid period).
        import numpy as np

        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [SimpleNamespace(entry_id="entry_c")]
            ),
            loop=self._loop,
        )
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 4, minutes=5)  # 5-min periods
        raw_high = np.array([1.5, 1.5])
        raw_low = np.array([0.0, 0.0])
        real_changes = 0
        prev_commanded = None
        for i in range(10):
            now = start + timedelta(minutes=5 * i)
            kw = raw_high if i % 2 == 0 else raw_low
            plan = _fake_plan(sheddable=[_fake_load_plan("s3", kw)])
            solver_writer.apply_commanded_state_guard(plan, now, grid_times)
            state = self._read_state("entry_c", "s3")
            if prev_commanded is not None and state.commanded_state != prev_commanded:
                real_changes += 1
            prev_commanded = state.commanded_state
        self.assertLessEqual(real_changes, 1)

    def test_a_brief_dip_that_resumes_within_the_hold_window_never_commands_off(self):
        # nimbus issue #595 (Mark Purcell, real finding: the #534 heat
        # pump cycled on/off every 15-35 min the first live morning,
        # burning the daily activation cap, because a jagged adequacy
        # plan dips below threshold for one period even while the load
        # is genuinely still wanted on a few periods later). Real repro
        # shape: period 0 (now) dips to 0.0 kW, but period 1 (5 min
        # later, well inside the default 10-min hold window) is back up
        # to 0.65 kW -- this must never register as a real OFF.
        import numpy as np

        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [SimpleNamespace(entry_id="entry_d")]
            ),
            loop=self._loop,
        )
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 6, minutes=5)  # 5-min periods
        period_hours_arr = np.full(5, 5.0 / 60.0)

        # First solve: load genuinely wants on, establishes commanded ON.
        plan_on = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s4", np.array([0.65, 0.65, 0.65, 0.65, 0.65]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan_on, start, grid_times, period_hours_arr
        )
        self.assertTrue(self._read_state("entry_d", "s4").commanded_state)

        # Second solve, 5 min later: period 0 dips to 0.0, but period 1
        # (5 min ahead, inside the 10-min default hold window) is back
        # to 0.65 -- must stay commanded ON, not flip off.
        now2 = start + timedelta(minutes=5)
        plan_dip = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s4", np.array([0.0, 0.65, 0.0, 0.65, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan_dip, now2, grid_times, period_hours_arr
        )
        self.assertTrue(self._read_state("entry_d", "s4").commanded_state)

    def test_lookahead_never_accelerates_an_off_to_on_transition(self):
        # One-sidedness check: the #595 lookahead only suppresses a
        # premature OFF while already commanded ON. A load that is
        # currently OFF must not be pre-emptively commanded ON just
        # because a later period in the same plan wants it on soon.
        import numpy as np

        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [SimpleNamespace(entry_id="entry_e")]
            ),
            loop=self._loop,
        )
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 6, minutes=5)
        period_hours_arr = np.full(5, 5.0 / 60.0)

        # First solve: load genuinely off right now, establishes OFF.
        plan_off = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s5", np.array([0.0, 0.0, 0.65, 0.65, 0.65]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan_off, start, grid_times, period_hours_arr
        )
        self.assertFalse(self._read_state("entry_e", "s5").commanded_state)


class TestApplyCommandedStateGuardPlanForecast(unittest.TestCase):
    """nimbus issue #581: apply_commanded_state_guard() also publishes
    each load's own full per-period plan now, when period_hours_arr is
    given -- see LoadRunState's own plan_forecast/plan_delivered_kwh_
    forecast/plan_target_kwh/plan_shortfall_kwh/plan_earliest_period/
    plan_deadline_period/plan_nominal_kw fields."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def test_deferrable_load_publishes_its_full_plan(self):
        import numpy as np

        sub = _fake_subentry(
            "s_defer",
            "controllable_load",
            {
                "deferrable_target_kwh": 5.0,
                "deferrable_earliest_hour": 1.0,
                "deferrable_deadline_hour": 6.0,
            },
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf", subentries={"s_defer": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 48, minutes=30)  # 24h @ 30min
        period_hours_arr = np.full(len(grid_times), 0.5)
        power_kw = np.array([2.0, 2.0] + [0.0] * (len(grid_times) - 2))
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan("s_defer", power_kw, adequacy=True, shortfall_kwh=0.3)
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf", "s_defer")

        self.assertEqual(len(result.plan_forecast), len(grid_times))
        self.assertEqual(result.plan_forecast[0]["time"], grid_times[0].isoformat())
        self.assertEqual(result.plan_forecast[0]["value"], 2.0)
        self.assertEqual(result.plan_forecast[2]["value"], 0.0)

        # Cumulative delivered kWh: 2.0kW * 0.5h = 1.0 each of the first
        # two periods, flat after that.
        self.assertEqual(len(result.plan_delivered_kwh_forecast), len(grid_times))
        self.assertAlmostEqual(result.plan_delivered_kwh_forecast[0]["value"], 1.0)
        self.assertAlmostEqual(result.plan_delivered_kwh_forecast[1]["value"], 2.0)
        self.assertAlmostEqual(result.plan_delivered_kwh_forecast[-1]["value"], 2.0)

        self.assertEqual(result.plan_target_kwh, 5.0)
        self.assertEqual(result.plan_shortfall_kwh, 0.3)
        # 1.0 (1am) -> period 2, 6.0 (6am) -> period 12, same resolution
        # as build_controllable_loads()'s own test above.
        self.assertEqual(result.plan_earliest_period, 2)
        self.assertEqual(result.plan_deadline_period, 12)
        self.assertIsNone(result.plan_nominal_kw)

    def test_a_same_day_window_already_open_reports_earliest_period_zero(self):
        # nimbus issue #582's own real repro, re-verified against THIS
        # function's own duplicated window-resolution logic (see its own
        # comment on the known drift risk): earliest=6am, deadline=4pm,
        # `now`=06:01 -- _resolve_hour_to_period_index() alone would roll
        # earliest_period to TOMORROW 6am while deadline stays today,
        # which must not leak into plan_earliest_period even though the
        # window is genuinely open right now.
        import numpy as np

        sub = _fake_subentry(
            "s_inprogress",
            "controllable_load",
            {
                "deferrable_target_kwh": 2.0,
                "deferrable_earliest_hour": 6.0,
                "deferrable_deadline_hour": 16.0,
            },
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(
                        entry_id="entry_pf5", subentries={"s_inprogress": sub}
                    )
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 6, 1, tzinfo=_TZ)  # just past 6am
        grid_times = _grid(
            now.replace(hour=0, minute=0), 96, minutes=15
        )  # 24h @ 15min from midnight
        period_hours_arr = np.full(len(grid_times), 0.25)
        power_kw = np.full(len(grid_times), 0.65)
        plan = _fake_plan(
            adequacy=[_fake_load_plan("s_inprogress", power_kw, adequacy=True)]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf5", "s_inprogress")
        self.assertEqual(result.plan_earliest_period, 0)
        self.assertGreater(result.plan_deadline_period, 0)

    def test_sheddable_load_publishes_served_kw_and_its_nominal(self):
        import numpy as np

        sub = _fake_subentry(
            "s_shed", "controllable_load", {"sheddable_nominal_kw": 1.5}
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf2", subentries={"s_shed": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        period_hours_arr = np.full(len(grid_times), 5 / 60)
        served_kw = np.array([1.5, 0.8, 0.0, 1.5])
        plan = _fake_plan(sheddable=[_fake_load_plan("s_shed", served_kw)])
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf2", "s_shed")

        self.assertEqual(len(result.plan_forecast), 4)
        self.assertEqual(result.plan_forecast[1]["value"], 0.8)
        self.assertEqual(result.plan_nominal_kw, 1.5)
        # Deferrable-only fields stay untouched for a sheddable load.
        self.assertIsNone(result.plan_target_kwh)
        self.assertIsNone(result.plan_earliest_period)

    def test_forecast_refreshes_even_when_commanded_state_is_unchanged(self):
        # The real point of #581: unlike commanded_state's own change-
        # gated write, the plan forecast must be current every cycle.
        import numpy as np

        sub = _fake_subentry(
            "s_refresh", "controllable_load", {"sheddable_nominal_kw": 1.5}
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf3", subentries={"s_refresh": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        period_hours_arr = np.full(len(grid_times), 5 / 60)

        plan1 = _fake_plan(
            sheddable=[_fake_load_plan("s_refresh", np.array([1.5, 1.5, 1.5, 1.5]))]
        )
        solver_writer.apply_commanded_state_guard(
            plan1, now, grid_times, period_hours_arr
        )
        first = self._read_state("entry_pf3", "s_refresh")

        # Same on/off decision (stays ON), but a genuinely different
        # period-1 value in the new solve's own plan.
        plan2 = _fake_plan(
            sheddable=[_fake_load_plan("s_refresh", np.array([1.5, 0.9, 1.5, 1.5]))]
        )
        solver_writer.apply_commanded_state_guard(
            plan2, now + timedelta(minutes=5), grid_times, period_hours_arr
        )
        second = self._read_state("entry_pf3", "s_refresh")

        self.assertEqual(first.commanded_state, second.commanded_state)
        self.assertNotEqual(
            first.plan_forecast[1]["value"], second.plan_forecast[1]["value"]
        )
        self.assertEqual(second.plan_forecast[1]["value"], 0.9)

    def test_no_period_hours_arr_skips_plan_publishing_entirely(self):
        # Every pre-#581 caller (and every existing test in this file)
        # invokes apply_commanded_state_guard() with exactly 3 args --
        # confirms that remains a complete, unchanged no-op for the new
        # fields, not just "doesn't crash".
        import numpy as np

        sub = _fake_subentry("s_old", "controllable_load", {})
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf4", subentries={"s_old": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        plan = _fake_plan(sheddable=[_fake_load_plan("s_old", np.array([1.5, 1.5]))])
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        result = self._read_state("entry_pf4", "s_old")
        self.assertIsNone(result.plan_forecast)
        self.assertTrue(result.commanded_state)

    def test_plan_shadow_price_forecast_reads_the_lp_power_balance_duals(self):
        # nimbus issue #613 (Mark Purcell, item 1 of 3 -- exposure only):
        # each load's own plan_shadow_price_forecast is lambda(t) for
        # every period, straight off plan.duals's own power_balance_t{i}
        # keys -- real per-period values, not a flat copy of period 0.
        #
        # nimbus issue #685 (Mark Purcell): this test originally asserted
        # the RAW, un-scaled dual as the expected value -- exactly the
        # #662 bug (a third call site #662's own fix never touched),
        # confirmed live as an exact x12 (1/hours[i], these are 5-minute
        # periods) mismatch against sensor.nimbus_solver_battery_
        # forecast's own correctly-scaled shadow_price for the identical
        # periods. Expected values below are now each raw dual /
        # period_hours_arr[i] (0.0013/(5/60)=0.0156, etc), matching the
        # correction already applied to the sibling `shadow_price` field
        # near line ~7240.
        import numpy as np

        sub = _fake_subentry(
            "s_shadow", "controllable_load", {"deferrable_target_kwh": 5.0}
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf6", subentries={"s_shadow": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        period_hours_arr = np.full(len(grid_times), 5 / 60)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_shadow", np.array([2.0, 2.0, 2.0, 2.0]), adequacy=True
                )
            ]
        )
        plan.duals = {
            "power_balance_t0": 0.0013,
            "power_balance_t1": 0.0006,
            "power_balance_t2": 0.0021,
            # period 3 deliberately absent -- must default to 0.0, not
            # raise or repeat the last real value.
        }
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf6", "s_shadow")
        self.assertEqual(len(result.plan_shadow_price_forecast), 4)
        self.assertAlmostEqual(result.plan_shadow_price_forecast[0]["value"], 0.0156)
        self.assertAlmostEqual(result.plan_shadow_price_forecast[1]["value"], 0.0072)
        self.assertAlmostEqual(result.plan_shadow_price_forecast[2]["value"], 0.0252)
        self.assertEqual(result.plan_shadow_price_forecast[3]["value"], 0.0)

    def test_plan_status_reason_marginal_cost_text_is_also_hours_scaled(self):
        # nimbus issue #685 (Mark Purcell): plan_status_reason's own
        # "marginal cost X c/kWh" text is built from the same shared
        # _raw_shadow_price_series() plan_shadow_price_forecast reads --
        # this proves the fix covers BOTH published fields from the one
        # shared source, not just the series. Real numbers from Mark's
        # own report: raw dual 0.0072 (period 0) must read as 0.0072 /
        # (5/60) = 0.0864 $/kWh = 8.64 c/kWh in the status text, not the
        # pre-fix 0.72 c/kWh.
        import numpy as np

        sub = _fake_subentry(
            "s_reason", "controllable_load", {"deferrable_target_kwh": 5.0}
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf8", subentries={"s_reason": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        period_hours_arr = np.full(len(grid_times), 5 / 60)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_reason", np.array([2.0, 2.0, 2.0, 2.0]), adequacy=True
                )
            ]
        )
        plan.duals = {"power_balance_t0": 0.0072}
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf8", "s_reason")
        self.assertEqual(
            result.plan_status_reason, "running now, marginal cost 8.64 c/kWh"
        )

    def test_plan_shadow_price_forecast_defaults_to_all_zero_when_plan_has_no_duals(
        self,
    ):
        # The bare SimpleNamespace test fakes every other test in this
        # file already uses never set .duals at all -- must degrade to
        # an honest all-zero series, never raise AttributeError.
        import numpy as np

        sub = _fake_subentry("s_noduals", "controllable_load", {})
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(entry_id="entry_pf7", subentries={"s_noduals": sub})
                ]
            ),
            loop=self._loop,
        )
        now = datetime(2026, 9, 7, 0, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        period_hours_arr = np.full(len(grid_times), 5 / 60)
        plan = _fake_plan(
            sheddable=[_fake_load_plan("s_noduals", np.array([1.5, 1.5, 1.5, 1.5]))]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_pf7", "s_noduals")
        self.assertEqual(
            [e["value"] for e in result.plan_shadow_price_forecast], [0.0] * 4
        )


class TestApplyCommandedStateGuardThermalForecast(unittest.TestCase):
    """nimbus issues #609/#610/#611 (Mark Purcell, real production
    findings against #592's own first shipment): real wiring tests for
    apply_commanded_state_guard()'s thermal-forecast block -- none
    existed before this pass, only thermal_forecast.py's own pure
    functions were tested (test_thermal_forecast.py). Same
    _FakeRunStateStore/_make_running_loop harness as
    TestApplyCommandedStateGuard above; a load only reaches this block
    at all when it's in the adequacy list (deferrable) AND has both
    deferrable_done_entity/controllable_load_power_sensor configured AND
    period_hours_arr is given (see solver_writer.py's own `elif
    load_kind == "adequacy"` block)."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def _seed_state(self, hub_entry_id, subentry_id, state):
        async def _write():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            await store.async_write(subentry_id, state)

        asyncio.run(_write())

    def _hub(self, entry_id, subentry, states):
        return SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: [
                    SimpleNamespace(
                        entry_id=entry_id, subentries={subentry.subentry_id: subentry}
                    )
                ]
            ),
            states=SimpleNamespace(get=lambda eid: states.get(eid)),
            loop=self._loop,
        )

    def test_settled_idle_reading_becomes_the_new_anchor(self):
        # nimbus issue #609: the load is genuinely idle and has been for
        # a while (off_since=None -- never been on this session) --  the
        # live current_temperature reading is trustworthy and must both
        # seed the projection AND become the new last_idle_temperature.
        import numpy as np

        sub = _fake_subentry(
            "s_therm",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t1",
            "s_therm",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=55.2)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t1", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm", np.array([0.0, 0.0, 0.0, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t1", "s_therm")
        self.assertEqual(result.last_idle_temperature, 55.2)
        # Period 0 is idle (0.0kW) -- decays from the fresh 55.2 anchor.
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], 55.2 - 0.5 * 0.5, places=2
        )

    def test_currently_on_ignores_the_live_reading_and_keeps_the_old_anchor(self):
        # nimbus issue #609's own real finding: current_temperature reads
        # ~10-11 degC LOW while the compressor is actively running (a
        # device-side reporting artifact on the #534 SG Ready bridge) --
        # must never anchor a projection, and must never overwrite a
        # good last_idle_temperature with a corrupted in-run reading.
        import numpy as np

        sub = _fake_subentry(
            "s_therm2",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t2",
            "s_therm2",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=True,  # actively heating right now
                off_since=None,
                last_idle_temperature=50.0,
            ),
        )
        states = {
            # Real #609 shape: a depressed in-run reading, ~10 degC low.
            "water_heater.hws": _fake_water_heater_state(current_temperature=40.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t2", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        # Plan power already matches the configured max, so #611's own
        # override (tested separately below) is a no-op here -- keeps
        # this test isolated to #609's anchor-selection question alone.
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm2", np.array([3.7, 3.7, 3.7, 3.7]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t2", "s_therm2")
        # Never overwritten with the corrupted 40.0 in-run reading.
        self.assertEqual(result.last_idle_temperature, 50.0)
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], 50.0 + 3.7 * 0.5 * 8.0, places=2
        )

    def test_just_stopped_within_the_settling_window_still_uses_the_old_anchor(self):
        # nimbus issue #609: a run that stopped moments ago hasn't
        # settled yet (SETTLING_MINUTES=5) -- the live reading is still
        # untrustworthy even though currently_on has already flipped
        # False.
        import numpy as np

        sub = _fake_subentry(
            "s_therm3",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t3",
            "s_therm3",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=(now - timedelta(minutes=1)).timestamp(),  # 1 min ago
                last_idle_temperature=50.0,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=39.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t3", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm3", np.array([0.0, 0.0, 0.0, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t3", "s_therm3")
        self.assertEqual(result.last_idle_temperature, 50.0)
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], 50.0 - 0.5 * 0.5, places=2
        )

    def test_override_first_period_power_applies_during_a_595_hold_window(self):
        # nimbus issue #611: #595's own hold-window guard can keep
        # commanded_state ON while THIS cycle's freshly-solved
        # plan_forecast[0] itself has already dipped to 0.0 -- the
        # projection must reflect the real commanded power (the load's
        # own configured max), not the plan's own period-0 value, or the
        # chart shows the tank cooling while it's actually still being
        # heated.
        import numpy as np

        sub = _fake_subentry(
            "s_therm4",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t4",
            "s_therm4",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=50.0,
                # Already commanded ON from a prior solve, well outside
                # decide_commanded_state()'s own hysteresis window.
                commanded_state=True,
                commanded_since=(now - timedelta(minutes=30)).timestamp(),
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=50.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t4", sub, states)
        # 5-min grid, same shape as the existing #595 hold-window test:
        # period 0 dips to 0.0 but period 1 (well inside the default
        # 10-min hold window) is back up -- #595's lookahead keeps this
        # cycle's raw_new_state (and therefore commanded_state) True.
        grid_times = _grid(now, 6, minutes=5)
        period_hours_arr = np.full(5, 5.0 / 60.0)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm4", np.array([0.0, 0.65, 0.0, 0.65, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t4", "s_therm4")
        self.assertTrue(result.commanded_state)  # the hold window held
        # Without the #611 override this would DECAY (plan period 0 is
        # 0.0kW) -- confirms the real configured max_power_kw (3.7kW)
        # was used instead, a genuine heating gain.
        expected = 50.0 + 3.7 * (5.0 / 60.0) * 8.0
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], expected, places=2
        )

    def test_ceiling_temperature_from_the_water_heaters_own_setpoint_caps_it(self):
        # nimbus issue #610: "no ceiling: the projection keeps adding
        # heating_rate x kWh past the heater's own setpoint" -- read
        # straight off the water_heater entity's own live `temperature`
        # attribute, never invented, never a new wizard field.
        import numpy as np

        sub = _fake_subentry(
            "s_therm5",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t5",
            "s_therm5",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        # A genuine 60 degC setpoint -- period 0 alone (3.7kW * 0.5h *
        # 8 degC/kWh = 14.8 degC of gain) would otherwise land at 72.8,
        # well past it.
        states = {
            "water_heater.hws": _fake_water_heater_state(
                current_temperature=58.0, temperature=60.0
            )
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t5", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm5", np.array([3.7, 3.7, 3.7, 3.7]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t5", "s_therm5")
        self.assertEqual(result.temperature_forecast[0]["value"], 60.0)

    def test_no_ceiling_attribute_at_all_is_a_complete_no_op(self):
        # A water_heater with neither `temperature` nor `max_temp`
        # published -- must project uncapped, same as before #610.
        import numpy as np

        sub = _fake_subentry(
            "s_therm6",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t6",
            "s_therm6",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=58.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t6", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm6", np.array([3.7, 3.7, 3.7, 3.7]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t6", "s_therm6")
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], 58.0 + 3.7 * 0.5 * 8.0, places=2
        )

    def test_max_temp_wins_over_the_idle_eco_setpoint_for_the_ceiling(self):
        # nimbus issue #640 (Mark Purcell, real repro): idle in "eco",
        # `temperature` reads the 45 degC eco setpoint (the floor the
        # unit maintains BETWEEN runs) while `max_temp` (65) is the
        # unit's own real operating ceiling. Clamping at 45 swallowed a
        # genuine 2 kWh/16 degC reheat entirely. The ceiling must prefer
        # max_temp over temperature so a real reheat is still visible.
        import numpy as np

        sub = _fake_subentry(
            "s_therm8",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t8",
            "s_therm8",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        # Mark's real numbers: idle at 45 (eco setpoint), max_temp 65.
        # Period 0 alone (3.7kW * 0.5h * 8 degC/kWh = 14.8 degC of gain)
        # would land at 59.8 -- correctly still below the real 65 degC
        # ceiling, and nowhere near the old, wrong 45 degC clamp.
        states = {
            "water_heater.hws": _fake_water_heater_state(
                current_temperature=45.0, temperature=45.0, max_temp=65.0
            )
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t8", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm8", np.array([3.7, 3.7, 3.7, 3.7]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t8", "s_therm8")
        self.assertAlmostEqual(
            result.temperature_forecast[0]["value"], 45.0 + 3.7 * 0.5 * 8.0, places=2
        )
        # A later period, still heating, genuinely clamps at max_temp
        # (65) rather than the old 45 degC eco setpoint.
        self.assertEqual(result.temperature_forecast[-1]["value"], 65.0)

    def test_done_when_threshold_is_the_ceiling_when_no_entity_attribute_exists(self):
        # nimbus issue #640: "at minimum the load's own done_when
        # threshold" -- an entity that publishes neither temperature nor
        # max_temp still must not clamp a projection below its own
        # configured done line.
        import numpy as np

        sub = _fake_subentry(
            "s_therm9",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "deferrable_done_when": ">= 60",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        day_key = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t9",
            "s_therm9",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=day_key,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=58.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t9", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm9", np.array([3.7, 3.7, 3.7, 3.7]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t9", "s_therm9")
        self.assertEqual(result.temperature_forecast[0]["value"], 60.0)

    def test_relearn_with_no_real_recorder_available_sets_source_to_fallback(self):
        # nimbus issue #610: "the published attributes do not say which
        # [a learned rate from a default]" -- a day_key change triggers
        # a relearn; this bare test harness has no real recorder to
        # fetch history from (_async_fetch_thermal_history degrades to
        # an empty history exactly the way it does on a real HA install
        # whenever the recorder query itself fails), so the learner
        # falls all the way back to the #592-cited defaults and must
        # honestly label that as "fallback", not silently look identical
        # to a real learned rate.
        import numpy as np

        sub = _fake_subentry(
            "s_therm7",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        self._seed_state(
            "entry_t7",
            "s_therm7",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key="",  # never learned yet
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=55.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t7", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm7", np.array([0.0, 0.0, 0.0, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t7", "s_therm7")
        self.assertEqual(result.thermal_rates_source, "fallback")
        self.assertEqual(
            result.thermal_heating_rate_c_per_kwh,
            thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH,
        )
        self.assertEqual(result.thermal_rates_learned_day_key, now.strftime("%Y-%m-%d"))

    def test_a_day_key_already_stamped_by_an_older_learner_still_relearns(self):
        # nimbus issue #618 (Mark Purcell, real finding the day the
        # #609/#610/#611 learner redesign shipped): thermal_rates_
        # learned_day_key was already stamped TODAY by the OLD #592-era
        # learner before this release's code even landed -- the day-key
        # gate alone would silently skip relearning under the NEW
        # idle-to-idle logic until tomorrow, leaving thermal_rates_source
        # stuck at its never-learned "" default and the stale rate
        # carried forward. A never-yet-labeled thermal_rates_source ("")
        # must force a relearn regardless of the day key.
        import numpy as np

        sub = _fake_subentry(
            "s_therm8",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        today = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t8",
            "s_therm8",
            load_run_state.LoadRunState(
                # Already stamped TODAY by the pre-#618 learner, but
                # thermal_rates_source was never set (the field didn't
                # exist until this same release) -- the real #618 shape.
                thermal_rates_learned_day_key=today,
                thermal_heating_rate_c_per_kwh=8.0,
                thermal_idle_decay_c_per_hour=0.5,
                thermal_rates_source="",
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=55.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t8", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm8", np.array([0.0, 0.0, 0.0, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t8", "s_therm8")
        # The relearn ran (this bare harness has no real recorder, so it
        # falls back honestly) -- proven by thermal_rates_source no
        # longer being "".
        self.assertEqual(result.thermal_rates_source, "fallback")

    def test_an_already_labeled_source_on_the_same_day_does_not_relearn_again(self):
        # The flip side of the #618 fix: once thermal_rates_source has
        # been genuinely labeled (this cycle or an earlier one today),
        # the once-per-day gate still holds -- must not re-fetch
        # recorder history on every single solve.
        import numpy as np

        sub = _fake_subentry(
            "s_therm9",
            "controllable_load",
            {
                "deferrable_done_entity": "water_heater.hws",
                "controllable_load_power_sensor": "sensor.hws_power",
                "deferrable_max_power_kw": 3.7,
            },
        )
        now = datetime(2026, 9, 9, 8, 0, tzinfo=_TZ)
        today = now.strftime("%Y-%m-%d")
        self._seed_state(
            "entry_t9",
            "s_therm9",
            load_run_state.LoadRunState(
                thermal_rates_learned_day_key=today,
                thermal_heating_rate_c_per_kwh=1.2,
                thermal_idle_decay_c_per_hour=0.35,
                thermal_rates_source="learned",
                currently_on=False,
                off_since=None,
                last_idle_temperature=None,
            ),
        )
        states = {
            "water_heater.hws": _fake_water_heater_state(current_temperature=55.0)
        }
        solver_writer._NATIVE_HASS = self._hub("entry_t9", sub, states)
        grid_times = _grid(now, 4, minutes=30)
        period_hours_arr = np.full(len(grid_times), 0.5)
        plan = _fake_plan(
            adequacy=[
                _fake_load_plan(
                    "s_therm9", np.array([0.0, 0.0, 0.0, 0.0]), adequacy=True
                )
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, grid_times, period_hours_arr
        )
        result = self._read_state("entry_t9", "s_therm9")
        # Untouched -- the seeded real rates survive, not overwritten by
        # a fallback from this bare harness's own empty recorder fetch.
        self.assertEqual(result.thermal_rates_source, "learned")
        self.assertEqual(result.thermal_heating_rate_c_per_kwh, 1.2)
        self.assertEqual(result.thermal_idle_decay_c_per_hour, 0.35)


class _FakeServiceCalls:
    """Records every hass.services.async_call() invocation -- real
    async_call() signature is (domain, service, service_data, ...,
    blocking=...); recorded as a plain tuple so tests can assert the
    exact domain/service/entity_id/mode dispatched, same reasoning as
    this file's own _FakeRunStateStore standing in for the real Store."""

    def __init__(self, raise_for: set[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise_for = raise_for or set()

    async def async_call(self, domain, service, service_data, **kwargs):
        if domain in self._raise_for:
            raise RuntimeError(f"simulated {domain} service failure")
        self.calls.append((domain, service, dict(service_data)))


class TestDispatchCommandedState(unittest.TestCase):
    """nimbus issue #476/#534: real tests for the output/actuation layer
    itself -- dispatch_commanded_state()'s own domain-pluggable service
    call, and apply_commanded_state_guard()'s wiring of it (real dispatch
    only on a genuine commanded_state transition, the daily activation
    cap, and per-load min_hold_minutes)."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def _hass(self, subentries, entry_id="entry_d", raise_for=None):
        services = _FakeServiceCalls(raise_for=raise_for)
        entry = SimpleNamespace(
            entry_id=entry_id, subentries={s.subentry_id: s for s in subentries}
        )
        return (
            SimpleNamespace(
                config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
                services=services,
                loop=self._loop,
                # nimbus issue #645: a real HomeAssistant instance always
                # has .states -- _resolve_controllable_load_tuning()
                # reads it (no live tuning number entities configured
                # here, a clean no-op returning None for everything, the
                # correct behaviour for every existing test in this
                # class, none of which exercise #645's own live-override
                # path).
                states=SimpleNamespace(get=lambda eid: None),
            ),
            services,
        )

    def test_switch_domain_load_turning_on_calls_switch_turn_on(self):
        import numpy as np

        sub = _fake_subentry(
            "s_sw",
            "controllable_load",
            {"controllable_load_device_entity": "switch.pool_pump"},
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        plan = _fake_plan(sheddable=[_fake_load_plan("s_sw", np.array([1.5, 1.5]))])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)

        self.assertEqual(len(services.calls), 1)
        domain, service, data = services.calls[0]
        self.assertEqual(domain, "switch")
        self.assertEqual(service, "turn_on")
        self.assertEqual(data["entity_id"], "switch.pool_pump")

    def test_switch_domain_load_turning_off_calls_switch_turn_off(self):
        import numpy as np

        # "Off" is only a real, dispatchable TRANSITION once the load has
        # genuinely been on -- a fresh, never-sampled state already
        # defaults to commanded_state=False, so going straight to "off"
        # from nothing is a no-op, not a transition. Establish a real ON
        # first (min_hold_minutes=0 so the OFF that follows adopts on the
        # very next solve where it persists a second time).
        sub = _fake_subentry(
            "s_sw2",
            "controllable_load",
            {
                "controllable_load_device_entity": "switch.pool_pump",
                "controllable_load_min_hold_minutes": 0,
            },
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 4, minutes=5)
        on_plan = _fake_plan(sheddable=[_fake_load_plan("s_sw2", np.array([1.5, 1.5]))])
        off_plan = _fake_plan(
            adequacy=[_fake_load_plan("s_sw2", np.array([0.0, 0.0]), adequacy=True)]
        )
        solver_writer.apply_commanded_state_guard(on_plan, start, grid_times)
        solver_writer.apply_commanded_state_guard(
            off_plan, start + timedelta(minutes=5), grid_times
        )
        solver_writer.apply_commanded_state_guard(
            off_plan, start + timedelta(minutes=10), grid_times
        )

        self.assertEqual(len(services.calls), 2)
        domain, service, _data = services.calls[-1]
        self.assertEqual(domain, "switch")
        self.assertEqual(service, "turn_off")

    def test_unchanged_commanded_state_across_solves_dispatches_only_once(self):
        # The real safety property: a load that STAYS on across many
        # re-solves must not spam switch.turn_on every cycle.
        import numpy as np

        sub = _fake_subentry(
            "s_stay",
            "controllable_load",
            {"controllable_load_device_entity": "switch.stays_on"},
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        for i in range(5):
            plan = _fake_plan(
                sheddable=[_fake_load_plan("s_stay", np.array([1.5, 1.5]))]
            )
            solver_writer.apply_commanded_state_guard(
                plan, now + timedelta(minutes=5 * i), grid_times
            )
        self.assertEqual(len(services.calls), 1)

    def test_water_heater_domain_load_turning_on_calls_set_operation_mode_performance(
        self,
    ):
        import numpy as np

        sub = _fake_subentry(
            "s_wh",
            "controllable_load",
            {
                "controllable_load_device_entity": (
                    "water_heater.hot_water_heat_pump_hot_water_sg_ready"
                )
            },
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        plan = _fake_plan(sheddable=[_fake_load_plan("s_wh", np.array([0.55, 0.55]))])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)

        self.assertEqual(len(services.calls), 1)
        domain, service, data = services.calls[0]
        self.assertEqual(domain, "water_heater")
        self.assertEqual(service, "set_operation_mode")
        self.assertEqual(
            data["entity_id"], "water_heater.hot_water_heat_pump_hot_water_sg_ready"
        )
        self.assertEqual(data["operation_mode"], "performance")

    def test_water_heater_domain_load_turning_off_calls_set_operation_mode_eco(self):
        import numpy as np

        # Same "establish a real ON first" reasoning as the switch-domain
        # OFF test above -- see its own comment.
        sub = _fake_subentry(
            "s_wh2",
            "controllable_load",
            {
                "controllable_load_device_entity": "water_heater.hws",
                "controllable_load_min_hold_minutes": 0,
            },
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 4, minutes=5)
        on_plan = _fake_plan(
            sheddable=[_fake_load_plan("s_wh2", np.array([0.55, 0.55]))]
        )
        off_plan = _fake_plan(
            adequacy=[_fake_load_plan("s_wh2", np.array([0.0, 0.0]), adequacy=True)]
        )
        solver_writer.apply_commanded_state_guard(on_plan, start, grid_times)
        solver_writer.apply_commanded_state_guard(
            off_plan, start + timedelta(minutes=5), grid_times
        )
        solver_writer.apply_commanded_state_guard(
            off_plan, start + timedelta(minutes=10), grid_times
        )

        self.assertEqual(len(services.calls), 2)
        _domain, _service, data = services.calls[-1]
        self.assertEqual(data["operation_mode"], "eco")

    def test_no_device_entity_configured_never_dispatches(self):
        import numpy as np

        sub = _fake_subentry("s_nodevice", "controllable_load", {})
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        plan = _fake_plan(
            sheddable=[_fake_load_plan("s_nodevice", np.array([1.5, 1.5]))]
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        self.assertEqual(len(services.calls), 0)
        # commanded_state is still tracked normally -- only dispatch is
        # skipped, matching this field's own no-op convention.
        result = self._read_state("entry_d", "s_nodevice")
        self.assertTrue(result.commanded_state)

    def test_max_activations_per_day_blocks_dispatch_once_reached(self):
        import numpy as np

        sub = _fake_subentry(
            "s_cap",
            "controllable_load",
            {
                "controllable_load_device_entity": "switch.capped",
                "controllable_load_max_activations_per_day": 1,
                # A tight min_hold so consecutive re-solves in this test
                # (5 min apart, same as the real grid) can each adopt a
                # new raw value on the very next solve rather than being
                # absorbed by the shared 2-period debounce.
                "controllable_load_min_hold_minutes": 0,
            },
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(start, 4, minutes=5)
        high = np.array([1.5, 1.5])
        low = np.array([0.0, 0.0])
        # decide_commanded_state()'s own debounce needs a NEW disagreeing
        # value to persist across two consecutive solves before it
        # adopts, even with min_hold_minutes=0 (the first occurrence only
        # starts the challenge) -- so [high, low, low, high, high]
        # produces exactly two real transitions: ON at solve 1 (the
        # very-first-ever decision, always immediate), OFF at solve 3
        # (low persisting a second time), ON again at solve 5 (high
        # persisting a second time). Cap=1 must block that second ON.
        sequence = [high, low, low, high, high]
        for i, kw in enumerate(sequence):
            plan = _fake_plan(sheddable=[_fake_load_plan("s_cap", kw)])
            solver_writer.apply_commanded_state_guard(
                plan, start + timedelta(minutes=5 * i), grid_times
            )
        on_calls = [c for c in services.calls if c[1] == "turn_on"]
        self.assertEqual(len(on_calls), 1)
        # The second requested ON is still recorded as the solver's own
        # desired commanded_state, even though it wasn't dispatched.
        result = self._read_state("entry_d", "s_cap")
        self.assertTrue(result.commanded_state)
        self.assertEqual(result.activations_today, 1)

    def test_min_hold_minutes_overrides_the_shared_default_hysteresis(self):
        import numpy as np

        # min_hold_minutes=0 means even a single-solve flip should adopt
        # immediately, unlike the shared 2-period default this same
        # 5-min grid would otherwise require.
        sub = _fake_subentry(
            "s_fast",
            "controllable_load",
            {
                "controllable_load_device_entity": "switch.fast",
                "controllable_load_min_hold_minutes": 0,
            },
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        plan = _fake_plan(sheddable=[_fake_load_plan("s_fast", np.array([1.5, 1.5]))])
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        result = self._read_state("entry_d", "s_fast")
        self.assertTrue(result.commanded_state)
        self.assertEqual(len(services.calls), 1)

    def test_a_dispatch_failure_does_not_raise_or_block_other_loads(self):
        import numpy as np

        sub_bad = _fake_subentry(
            "s_bad",
            "controllable_load",
            {"controllable_load_device_entity": "switch.will_fail"},
        )
        sub_good = _fake_subentry(
            "s_good",
            "controllable_load",
            {"controllable_load_device_entity": "switch.will_succeed"},
        )
        solver_writer._NATIVE_HASS, _services = self._hass(
            [sub_bad, sub_good], raise_for={"switch"}
        )
        # raise_for covers BOTH switches (same domain) -- confirms the
        # whole-function try/except doesn't get triggered by the first
        # load's failure in a way that skips the second load entirely;
        # the test asserts on state persistence, not on a call succeeding.
        plan = _fake_plan(
            sheddable=[
                _fake_load_plan("s_bad", np.array([1.5, 1.5])),
                _fake_load_plan("s_good", np.array([1.5, 1.5])),
            ]
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        # Must not raise.
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        # Both loads' commanded_state is still persisted despite the
        # dispatch failure -- the guard's own bookkeeping is independent
        # of whether the physical command actually succeeded.
        self.assertTrue(self._read_state("entry_d", "s_bad").commanded_state)
        self.assertTrue(self._read_state("entry_d", "s_good").commanded_state)

    def test_climate_domain_logs_a_warning_and_does_not_raise(self):
        import numpy as np

        sub = _fake_subentry(
            "s_climate",
            "controllable_load",
            {"controllable_load_device_entity": "climate.living_room"},
        )
        solver_writer._NATIVE_HASS, services = self._hass([sub])
        plan = _fake_plan(
            sheddable=[_fake_load_plan("s_climate", np.array([1.5, 1.5]))]
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        grid_times = _grid(now, 4, minutes=5)
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as logs:
            solver_writer.apply_commanded_state_guard(plan, now, grid_times)
        self.assertTrue(any("unsupported domain" in r.message for r in logs.records))
        self.assertEqual(len(services.calls), 0)
        # commanded_state is still tracked -- only the physical dispatch
        # is a no-op for an unsupported domain.
        result = self._read_state("entry_d", "s_climate")
        self.assertTrue(result.commanded_state)


class TestFlipFloppingRawDecisionNeverActivatesHws(unittest.TestCase):
    """nimbus issue #741 (Mark Purcell, real household finding): a pseudo
    water_heater device standing in for the real WWK302, driven through
    apply_commanded_state_guard() the same way TestDispatchCommandedState
    above does -- built to answer Mark's own real question, "are HWS
    controllable loads actually activated, or just repeatedly scheduled
    and deferred?"

    Root cause confirmed live 2026-09-11: sensor.nimbus_hot_water_heat_
    pump_next_start promised 7 distinct start times between midnight and
    08:50 local, none of which produced a real activation --
    commanded_since stayed pinned at 16:20 the PREVIOUS day the entire
    time. decide_commanded_state()'s own debounce (load_run_state.py)
    only adopts a disagreeing raw_new_state once it has held
    CONSECUTIVELY for min_hold_minutes; a value that flips back even
    once resets the challenge to zero. If the LP's own period-0 raw
    on/off decision flips every solve (which is exactly what the real
    household's own "scheduled 08:00-14:30" / "scheduled 08:30-15:00"
    back-and-forth status history showed happening overnight), the
    challenge clock can never accumulate min_hold_minutes of unbroken
    agreement -- the load is stuck "always about to start," forever.

    Uses the real HWS-shaped config (water_heater domain,
    min_hold_minutes=15 -- the household's own live value) and the same
    5-minute solve cadence the real coordinator uses, so this reproduces
    the true real-world timescale rather than an artificially fast one.
    """

    _DEVICE_ENTITY = "water_heater.test_hws"
    _MIN_HOLD_MINUTES = 15

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _hws_hass(self, min_hold_minutes=_MIN_HOLD_MINUTES):
        sub = _fake_subentry(
            "s_hws",
            "controllable_load",
            {
                "controllable_load_device_entity": self._DEVICE_ENTITY,
                "controllable_load_min_hold_minutes": min_hold_minutes,
            },
        )
        hass, services = self._hass([sub])
        solver_writer._NATIVE_HASS = hass
        return services

    def _solve(self, on: bool, now):
        import numpy as np

        kw = np.array([0.65, 0.65]) if on else np.array([0.0, 0.0])
        plan = _fake_plan(adequacy=[_fake_load_plan("s_hws", kw, adequacy=True)])
        grid_times = _grid(now, 4, minutes=5)
        solver_writer.apply_commanded_state_guard(plan, now, grid_times)

    def test_period0_flip_flopping_every_solve_never_activates_the_device(self):
        """The exact #741 shape: the raw decision alternates on/off every
        single 5-minute solve, for far longer than min_hold_minutes (15).
        A household watching this would see repeated "next start in 5
        minutes" promises -- and the pseudo device must never actually
        receive water_heater.set_operation_mode("performance"), because
        no single value ever holds consecutively long enough to adopt.
        """
        services = self._hws_hass()
        start = datetime(2026, 9, 11, 0, 0, tzinfo=_TZ)
        # decide_commanded_state()'s own very-first-ever-decision rule
        # adopts immediately (no prior commitment to protect yet) -- so
        # establish the real baseline first: the load starts OFF, exactly
        # like the real household's own commanded_since pinned at
        # yesterday 16:20. Everything from here on is a genuine
        # DEBOUNCED challenge, the same position #741 was found in.
        self._solve(on=False, now=start)
        services.calls.clear()

        # 24 further solves at 5min spacing = 2 real hours of continuous
        # flip-flopping, well past the 15-minute hysteresis window --
        # if the guard were even briefly stable this would activate.
        for i in range(24):
            self._solve(on=(i % 2 == 0), now=start + timedelta(minutes=5 * (i + 1)))

        self.assertEqual(
            services.calls,
            [],
            "device received a real command despite the raw decision never "
            "holding steady for min_hold_minutes -- the #741 guard "
            "invariant is broken",
        )
        # The household-visible commanded_state itself must also still
        # read "off" -- not just "no service call happened to fire", but
        # the actual published decision the Status/next_start sensors are
        # built from never adopted ON either.
        result = self._read_state("entry_d", "s_hws")
        self.assertFalse(result.commanded_state)

    def test_period0_stabilising_after_false_starts_eventually_activates(self):
        """Companion/control case: the same pseudo device, the same
        false-start pattern for the first few cycles (proving this test
        exercises the identical mechanism as the failure case above), but
        the raw decision then genuinely settles on ON and HOLDS -- the
        guard must recover and dispatch exactly once, confirming the
        debounce is a real delay, not a permanent lock-out once it has
        already been challenged unsuccessfully."""
        services = self._hws_hass()
        start = datetime(2026, 9, 11, 0, 0, tzinfo=_TZ)
        # Same real baseline as the failure test above: establish OFF as
        # the very-first-ever decision before any debounced challenge.
        self._solve(on=False, now=start)
        services.calls.clear()

        # Four false starts (matches the real household's own morning:
        # repeated promised-then-abandoned activations)...
        for i in range(4):
            self._solve(on=(i % 2 == 0), now=start + timedelta(minutes=5 * (i + 1)))
        self.assertEqual(services.calls, [])

        # ...then the plan genuinely commits: ON, consistently, for long
        # enough to cross the 15-minute hysteresis (4 more solves x 5min
        # = 20 real minutes of unbroken agreement).
        settle_start = start + timedelta(minutes=5 * 5)
        for i in range(4):
            self._solve(on=True, now=settle_start + timedelta(minutes=5 * i))

        self.assertEqual(len(services.calls), 1)
        domain, service, data = services.calls[0]
        self.assertEqual(domain, "water_heater")
        self.assertEqual(service, "set_operation_mode")
        self.assertEqual(data["entity_id"], self._DEVICE_ENTITY)
        self.assertEqual(data["operation_mode"], "performance")

        result = self._read_state("entry_d", "s_hws")
        self.assertTrue(result.commanded_state)

    def _read_state(self, hub_entry_id, subentry_id):
        async def _read():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{hub_entry_id}_load_run_state"
                )
            )
            return await store.async_read(subentry_id)

        return asyncio.run(_read())

    def _hass(self, subentries, entry_id="entry_d", raise_for=None):
        services = _FakeServiceCalls(raise_for=raise_for)
        entry = SimpleNamespace(
            entry_id=entry_id, subentries={s.subentry_id: s for s in subentries}
        )
        return (
            SimpleNamespace(
                config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
                services=services,
                loop=self._loop,
                states=SimpleNamespace(get=lambda eid: None),
            ),
            services,
        )


if __name__ == "__main__":
    unittest.main()
