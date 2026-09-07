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


def _fake_native_hass(subentries: list):
    entry = SimpleNamespace(subentries={s.subentry_id: s for s in subentries})
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry])
    )


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


def _fake_state(value):
    return SimpleNamespace(state=value)


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


if __name__ == "__main__":
    unittest.main()
