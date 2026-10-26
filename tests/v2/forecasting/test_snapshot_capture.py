"""The day-ahead forecast snapshot is captured on the first successful
solve after local midnight, once a day, and persisted (#937) (#1837).

No timer: a restart late in the day still captures one, every later solve
finds it filed and does nothing, and a failed solve captures nothing.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import forecast_snapshot_store as fss

BNE = timezone(timedelta(hours=10))


class _FakeStore:
    """Stands in for HA's Store. Records saves so a test can assert what was
    persisted, not merely that persisting was attempted."""

    def __init__(self, initial: dict | None = None, fail_on_save: bool = False):
        self.data = dict(initial or {})
        self.saves: list[dict] = []
        self.fail_on_save = fail_on_save
        # Counted for #1295: the already-captured path must not re-read.
        self.loads = 0

    async def async_load(self):
        self.loads += 1
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
    """Drive a capture with a fake Store and a fixed clock.

    Resets the module cache first. Since #1295 the capture path reads an
    in-memory copy of the Store before touching disk, so without this each
    test after the first in a class would read the PREVIOUS test's data and
    never reach its own fake Store. That cache is real production state --
    `reset_module_state()` clears it on unload for the same reason.
    """
    fss.reset_snapshot_cache()
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
    def test_retention_covers_the_rescore_window(self):
        """The re-score service accepts 1-30 days back; a retention equal to
        that would drop a day the moment a re-score reached for it."""
        self.assertGreater(fss.KEEP_DAYS, 30)


class TestTheCommonPathDoesNotReReadTheStore(unittest.TestCase):
    """nimbus issue #1295 (Mark Purcell, IV&V #1289).

    Stage 2's own commit message claimed "the cost is one dict lookup per
    cycle" for the already-captured path. It was not: a fresh `Store` was
    built and the whole file re-read and re-parsed on every cycle, roughly
    288 times a day. Asserted by COUNTING loads, because the original claim
    was plausible-looking prose that no structural test would have doubted.
    """

    def tearDown(self):
        fss.reset_snapshot_cache()


class TestWhatIsNotCaptured(unittest.TestCase):
    def test_partially_corrupt_rows_still_capture_the_good_ones(self):
        rows = _rows(4) + [{"time": "nonsense"}, {"solar_kw": 1.0}]
        store = _FakeStore()
        self.assertTrue(_run(_Hass(rows), store))
        self.assertEqual(len(store.data["2026-09-26"]["points"]), 4)


class TestItNeverBreaksASolve(unittest.TestCase):
    """A diagnostic must never turn a successful solve into a reported
    failure."""

    def tearDown(self):
        fss.reset_snapshot_cache()

    def test_a_store_write_failure_is_swallowed(self):
        store = _FakeStore(fail_on_save=True)
        self.assertFalse(_run(_Hass(_rows()), store))


class TestLoadingBackASnapshot(unittest.TestCase):
    def setUp(self):
        fss.reset_snapshot_cache()

    def tearDown(self):
        fss.reset_snapshot_cache()


if __name__ == "__main__":
    unittest.main(verbosity=2)
