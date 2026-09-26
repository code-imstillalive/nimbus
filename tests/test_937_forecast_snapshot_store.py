"""nimbus issue #937, stage 2: one snapshot per local day, persisted.

Stage 1 built the pure shape. This gives it somewhere to live and a moment to
be written, and wires it into the solve cycle.

## The capture moment, and why it is not a timer

The first **successful** solve after local midnight. That is the closest a
1-minute solver gets to "what the forecast said about today before today
happened", and it needs no new trigger:

* **self-healing** — a restart at 09:00 on a day with no snapshot still
  captures one, late but real, instead of losing the day;
* **idempotent** — every later solve that day finds one filed and does nothing;
* **no fixed-time job** — #1217 is this repo's own reminder of what one of
  those does on every install at once.

`captured_at` is stored so a consumer can tell a true midnight capture from a
post-restart one, because a snapshot taken at 09:00 has already seen nine hours
of the day it forecasts and will flatter itself.

## Only after a genuine success

A failed cycle has published nothing, so there is no forecast to capture —
filing an empty one would record a forecast the solver never made, which then
scores as a real forecast error. `TestOnlyASuccessfulSolveCaptures` pins that.

## Failure posture

Everything is swallowed. A capture is a diagnostic: a solve that succeeded must
never be reported as failed because a Store write did not work.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import forecast_snapshot_store as fss

BNE = timezone(timedelta(hours=10))


class _FakeStore:
    """Stands in for HA's Store. Records saves so a test can assert what was
    persisted, not merely that persisting was attempted."""

    def __init__(self, initial: dict | None = None, fail_on_save: bool = False):
        self.data = dict(initial or {})
        self.saves: list[dict] = []
        self.fail_on_save = fail_on_save

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, data):
        if self.fail_on_save:
            raise OSError("disk full")
        self.saves.append(data)
        self.data = dict(data)


class _State:
    def __init__(self, attributes):
        self.attributes = attributes


class _States:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, entity_id):
        return self._m.get(entity_id)


class _Hass:
    def __init__(self, rows=None):
        attrs = {"forecast": rows} if rows is not None else {}
        self.states = _States(
            {fss._SOURCE_ENTITY: _State(attrs)} if rows is not None else {}
        )


def _lenient_parse(value):
    """Mirror `homeassistant.util.dt.parse_datetime`: None, never a raise."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _rows(n=6, start=None):
    start = start or datetime(2026, 9, 26, 0, 0, tzinfo=BNE)
    return [
        {
            "time": (start + timedelta(minutes=5 * i)).isoformat(),
            "solar_kw": float(i),
            "load_kw": 1.0 + i,
        }
        for i in range(n)
    ]


def _run(hass, store, *, now=None, entry="hub1"):
    """Drive a capture with a fake Store and a fixed clock."""
    now = now or datetime(2026, 9, 26, 0, 1, tzinfo=BNE)
    real_store_for, real_now = fss._store_for, fss.dt_util.now
    real_parse = fss.dt_util.parse_datetime
    fss._store_for = lambda _h, _e: store
    fss.dt_util.now = lambda: now
    # Returns None on bad input, like real HA's dt_util.parse_datetime.
    # An earlier version of this double RAISED instead, which made a
    # correct-code test fail and nearly led to "fixing" the production path.
    fss.dt_util.parse_datetime = _lenient_parse
    try:
        return asyncio.run(fss.async_capture_todays_snapshot(hass, entry))
    finally:
        fss._store_for, fss.dt_util.now = real_store_for, real_now
        fss.dt_util.parse_datetime = real_parse


class TestCapturingTodaysSnapshot(unittest.TestCase):
    def test_a_first_solve_of_the_day_captures(self):
        store = _FakeStore()
        self.assertTrue(_run(_Hass(_rows()), store))
        self.assertIn("2026-09-26", store.data)
        self.assertEqual(len(store.data["2026-09-26"]["points"]), 6)

    def test_a_later_solve_the_same_day_is_a_no_op(self):
        """Idempotent -- the cost on every cycle after the first is one dict
        lookup, not a rewrite of the whole Store."""
        store = _FakeStore({"2026-09-26": {"version": 1, "points": []}})
        self.assertFalse(_run(_Hass(_rows()), store))
        self.assertEqual(store.saves, [])

    def test_a_new_day_captures_again(self):
        store = _FakeStore({"2026-09-25": {"version": 1, "points": []}})
        self.assertTrue(_run(_Hass(_rows()), store))
        self.assertIn("2026-09-26", store.data)
        self.assertIn("2026-09-25", store.data)

    def test_it_records_when_it_was_captured(self):
        """So a consumer can tell a true midnight capture from a
        post-restart one that has already seen half the day it forecasts."""
        store = _FakeStore()
        late = datetime(2026, 9, 26, 9, 30, tzinfo=BNE)
        _run(_Hass(_rows()), store, now=late)
        self.assertEqual(store.data["2026-09-26"]["captured_at"], late.isoformat())

    def test_retention_is_applied_on_write(self):
        """Pruned on write, not on read: this Store is rewritten whole, so an
        unbounded dict would be re-serialised on every capture."""
        old = {f"2026-08-{d:02d}": {"version": 1, "points": []} for d in range(1, 29)}
        old.update(
            {f"2026-09-{d:02d}": {"version": 1, "points": []} for d in range(1, 26)}
        )
        store = _FakeStore(old)
        _run(_Hass(_rows()), store)
        self.assertLessEqual(len(store.data), fss.KEEP_DAYS)
        self.assertIn("2026-09-26", store.data)

    def test_retention_covers_the_rescore_window(self):
        """The re-score service accepts 1-30 days back; a retention equal to
        that would drop a day the moment a re-score reached for it."""
        self.assertGreater(fss.KEEP_DAYS, 30)


class TestWhatIsNotCaptured(unittest.TestCase):
    def test_no_published_forecast_captures_nothing(self):
        store = _FakeStore()
        self.assertFalse(_run(_Hass(None), store))
        self.assertEqual(store.saves, [])

    def test_an_empty_forecast_array_captures_nothing(self):
        store = _FakeStore()
        self.assertFalse(_run(_Hass([]), store))
        self.assertEqual(store.saves, [])

    def test_rows_with_no_usable_point_capture_nothing(self):
        """Filing an empty snapshot would record a forecast the solver never
        made, which then scores as a real forecast error."""
        store = _FakeStore()
        self.assertFalse(_run(_Hass([{"junk": 1}, "not a dict"]), store))
        self.assertEqual(store.saves, [])

    def test_partially_corrupt_rows_still_capture_the_good_ones(self):
        rows = _rows(4) + [{"time": "nonsense"}, {"solar_kw": 1.0}]
        store = _FakeStore()
        self.assertTrue(_run(_Hass(rows), store))
        self.assertEqual(len(store.data["2026-09-26"]["points"]), 4)


class TestItNeverBreaksASolve(unittest.TestCase):
    """A diagnostic must never turn a successful solve into a reported
    failure."""

    def test_a_store_write_failure_is_swallowed(self):
        store = _FakeStore(fail_on_save=True)
        self.assertFalse(_run(_Hass(_rows()), store))

    def test_a_load_failure_is_swallowed(self):
        class _Boom:
            async def async_load(self):
                raise OSError("unreadable")

            async def async_save(self, data):
                raise AssertionError("must not be reached")

        self.assertFalse(_run(_Hass(_rows()), _Boom()))


class TestLoadingBackASnapshot(unittest.TestCase):
    def test_a_known_day_comes_back(self):
        store = _FakeStore({"2026-09-26": {"version": 1, "points": [1]}})
        real = fss._store_for
        fss._store_for = lambda _h, _e: store
        try:
            got = asyncio.run(fss.async_load_snapshot(_Hass(), "hub1", "2026-09-26"))
        finally:
            fss._store_for = real
        self.assertEqual(got, {"version": 1, "points": [1]})

    def test_an_unknown_day_is_None_not_an_empty_snapshot(self):
        """None means "skip the decomposition entirely", never "assume zero
        forecast error" -- an empty snapshot would score as the worst
        forecast possible rather than as an absent one."""
        store = _FakeStore({})
        real = fss._store_for
        fss._store_for = lambda _h, _e: store
        try:
            got = asyncio.run(fss.async_load_snapshot(_Hass(), "hub1", "2026-01-01"))
        finally:
            fss._store_for = real
        self.assertIsNone(got)


class TestOnlyASuccessfulSolveCaptures(unittest.TestCase):
    """Wiring, asserted on the source: a failed cycle published nothing, so
    capturing after it would file a forecast the solver never made."""

    def test_the_capture_is_called_from_async_run_solve(self):
        from custom_components.nimbus_load import solver_runtime

        src = inspect.getsource(solver_runtime.async_run_solve)
        self.assertIn("async_capture_todays_snapshot", src)

    def test_it_is_guarded_on_the_success_flag(self):
        from custom_components.nimbus_load import solver_runtime

        src = inspect.getsource(solver_runtime.async_run_solve)
        i = src.index("async_capture_todays_snapshot")
        self.assertIn("if ok:", src[max(0, i - 500) : i])

    def test_it_runs_on_the_event_loop_not_the_worker_thread(self):
        """A Store write must be awaited. _run_one_cycle() runs in an
        executor, so the capture deliberately sits in the async wrapper."""
        from custom_components.nimbus_load import solver_runtime

        self.assertNotIn(
            "async_capture_todays_snapshot",
            inspect.getsource(solver_runtime._run_one_cycle),
        )

    def test_a_single_entry_is_required_for_the_store_key(self):
        """The Store name is per-entry. Writing one install's forecast under
        another's key would be worse than not capturing it."""
        from custom_components.nimbus_load import solver_runtime

        class _Entries:
            def __init__(self, n):
                self._n = n

            def async_entries(self, _domain):
                return [type("E", (), {"entry_id": f"e{i}"}) for i in range(self._n)]

        class _H:
            def __init__(self, n):
                self.config_entries = _Entries(n)

        self.assertEqual(solver_runtime._entry_id_for_snapshot(_H(1)), "e0")
        self.assertIsNone(solver_runtime._entry_id_for_snapshot(_H(0)))
        self.assertIsNone(solver_runtime._entry_id_for_snapshot(_H(2)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
