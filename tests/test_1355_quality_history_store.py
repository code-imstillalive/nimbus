"""nimbus issue #1355, step 2: a durable, dated archive of each day's score.

What is pinned, and why each matters:

- a **provisional** midnight score is replaced by the **settled** rescore --
  keeping the first write would archive exactly the wrong number (the
  reference household read -44.23% at midnight and 73.0% after settlement for
  the same day);
- an unchanged score costs no write and no Store re-read (#1295's lesson);
- a failed save leaves memory matching disk, so the next cycle retries
  instead of believing the day is archived;
- retention prunes inside the dict -- never a file deletion, which is the
  #1324 backup race;
- the per-hour detail #1318 could not recover is actually in the record.

Same `_FakeStore`/`_run()` pattern as the #937/#1295 snapshot-store tests.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import quality_history_store as qhs

BNE = timezone(timedelta(hours=10))
_RUNTIME = Path(__file__).resolve().parent.parent / (
    "custom_components/nimbus_load/solver_runtime.py"
)


class _FakeStore:
    def __init__(self, initial=None, fail_save=False):
        self.data = dict(initial or {})
        self.saves: list[dict] = []
        self.loads = 0
        self.fail_save = fail_save

    async def async_load(self):
        self.loads += 1
        return dict(self.data)

    async def async_save(self, data):
        if self.fail_save:
            raise OSError("disk full")
        self.saves.append(data)
        self.data = dict(data)


class _State:
    def __init__(self, attributes):
        self.attributes = attributes


class _Hass:
    def __init__(self, attributes=None):
        self._state = _State(attributes) if attributes is not None else None

    @property
    def states(self):
        hass = self

        class _S:
            def get(self, entity_id):
                return hass._state if entity_id == qhs._SOURCE_ENTITY else None

        return _S()


def _report(
    day="2026-09-28", epr=0.73, j_ach=-3.8656, provisional=False, status="applied"
):
    """The real published shape, keys taken from the reference household's
    live `sensor.nimbus_solver_quality_report` on 2026-09-29."""
    row = {"epr": epr, "j_ach": j_ach, "v": "0.94.429"}
    if provisional:
        row["p"] = 1
    return {
        "latest_date": day,
        "epr": epr,
        "epr_pct": round(epr * 100, 2),
        "j_ref": 2.8034,
        "j_ach": j_ach,
        "j_star": -4.4938,
        "j_star_evaluator": -6.3325,
        "regret_dollars": 2.4669,
        "real_p2p_settlement_status": status,
        "nimbus_version": "0.94.429",
        "hourly_regret": {"15": 1.413, "14": 0.9287},
        "soc_discrepancy_hourly": [
            {
                "hour": f"{day}T16:00:00+10:00",
                "real_pct": 100.0,
                "ach_pct": 93.32,
                "gap_pct": 6.68,
            }
        ],
        "j_ref_hourly": [{"t": f"{day}T00:00:00+10:00"}],
        "j_ach_hourly": [{"t": f"{day}T00:00:00+10:00"}],
        "j_star_hourly": [{"t": f"{day}T00:00:00+10:00"}],
        "history": {day: row},
        "friendly_name": "not archived",
    }


def _run(hass, store, *, entry="hub1"):
    real_store_for, real_now = qhs._store_for, qhs.dt_util.now
    qhs._store_for = lambda _h, _e: store
    qhs.dt_util.now = lambda: datetime(2026, 9, 29, 6, 1, tzinfo=BNE)
    try:
        return asyncio.run(qhs.async_capture_quality_day(hass, entry))
    finally:
        qhs._store_for, qhs.dt_util.now = real_store_for, real_now


class _Base(unittest.TestCase):
    def setUp(self):
        qhs.reset_quality_history_cache()

    def tearDown(self):
        qhs.reset_quality_history_cache()


class TestBuild(_Base):
    def test_nothing_to_archive_without_a_dated_score(self):
        self.assertIsNone(qhs.build_quality_capture({}, captured_at="x"))
        self.assertIsNone(
            qhs.build_quality_capture({"latest_date": "2026-09-28"}, captured_at="x")
        )

    def test_the_per_hour_detail_1318_could_not_recover_is_kept(self):
        day, rec = qhs.build_quality_capture(_report(), captured_at="x")
        self.assertEqual(day, "2026-09-28")
        for k in (
            "hourly_regret",
            "soc_discrepancy_hourly",
            "j_ref_hourly",
            "j_ach_hourly",
            "j_star_hourly",
        ):
            self.assertIn(k, rec)
        self.assertEqual(rec["regret_dollars"], 2.4669)
        self.assertNotIn("friendly_name", rec, "only the explicit key list is archived")
        self.assertNotIn("history", rec, "the rolling attribute is what this outlives")

    def test_provisional_comes_from_the_rows_own_marker(self):
        _, prov = qhs.build_quality_capture(_report(provisional=True), captured_at="x")
        _, settled = qhs.build_quality_capture(_report(), captured_at="x")
        self.assertTrue(prov["provisional"])
        self.assertFalse(settled["provisional"])

    def test_captured_at_does_not_change_the_fingerprint(self):
        _, a = qhs.build_quality_capture(_report(), captured_at="06:00")
        _, b = qhs.build_quality_capture(_report(), captured_at="06:01")
        self.assertEqual(qhs.fingerprint(a), qhs.fingerprint(b))


class TestCapture(_Base):
    def test_first_sight_of_a_day_is_written(self):
        store = _FakeStore()
        self.assertTrue(_run(_Hass(_report()), store))
        self.assertEqual(list(store.data), ["2026-09-28"])

    def test_an_unchanged_score_costs_no_write_and_no_reread(self):
        store = _FakeStore()
        _run(_Hass(_report()), store)
        self.assertFalse(_run(_Hass(_report()), store))
        self.assertEqual(len(store.saves), 1)
        self.assertEqual(store.loads, 1, "#1295: the common cycle is a dict lookup")

    def test_the_settled_rescore_replaces_the_provisional_midnight_figure(self):
        """The reference household's real 28 Sep: -44.23% at 00:00, 73.0% at 06:00."""
        store = _FakeStore()
        _run(
            _Hass(
                _report(
                    epr=-0.4423,
                    j_ach=3.5599,
                    provisional=True,
                    status="no_settlement_entry_for_this_date",
                )
            ),
            store,
        )
        self.assertTrue(store.data["2026-09-28"]["provisional"])
        self.assertTrue(_run(_Hass(_report()), store))
        rec = store.data["2026-09-28"]
        self.assertFalse(rec["provisional"])
        self.assertEqual(rec["epr"], 0.73)
        self.assertEqual(
            len(store.data), 1, "one record per day, rewritten -- not a second row"
        )

    def test_a_failed_save_is_retried_next_cycle(self):
        """Same store, disk full on one cycle and fine on the next: the day
        must be written on the second cycle, not treated as already filed."""
        store = _FakeStore(fail_save=True)
        self.assertFalse(_run(_Hass(_report()), store))
        self.assertEqual(store.saves, [])
        store.fail_save = False
        self.assertTrue(_run(_Hass(_report()), store))
        self.assertEqual(list(store.data), ["2026-09-28"])

    def test_cache_is_not_updated_when_the_save_fails(self):
        _run(_Hass(_report()), _FakeStore(fail_save=True))
        self.assertNotIn("2026-09-28", qhs._CACHE.get("hub1", {}))

    def test_no_entity_or_no_score_writes_nothing(self):
        store = _FakeStore()
        self.assertFalse(_run(_Hass(None), store))
        self.assertFalse(_run(_Hass({"friendly_name": "x"}), store))
        self.assertEqual(store.saves, [])


class TestRetention(_Base):
    def test_prune_keeps_the_newest_days(self):
        stored = {
            f"2026-{m:02d}-{d:02d}": {} for m in (5, 6, 7, 8, 9) for d in range(1, 29)
        }
        kept = qhs.prune(stored, keep_days=qhs.KEEP_DAYS)
        self.assertEqual(len(kept), qhs.KEEP_DAYS)
        self.assertEqual(max(kept), "2026-09-28")
        self.assertEqual(min(kept), sorted(stored)[-qhs.KEEP_DAYS])

    def test_retention_outlives_the_reference_households_recorder_purge(self):
        """The point of the archive: 45-day recorder purge on 116KAT."""
        self.assertGreater(qhs.KEEP_DAYS, 45)

    def test_nothing_in_the_module_deletes_a_file(self):
        """#1324: unlinking inside /config races the backup walk."""
        src = Path(qhs.__file__).read_text(encoding="utf-8")
        for call in (
            "os.remove(",
            "os.unlink(",
            ".unlink(",
            "shutil.rmtree(",
            "async_remove(",
        ):
            self.assertNotIn(call, src)


class TestWiring(unittest.TestCase):
    def test_runtime_captures_after_a_successful_solve_and_resets_on_unload(self):
        src = _RUNTIME.read_text(encoding="utf-8")
        self.assertIn("await async_capture_quality_day(hass, entry_id)", src)
        self.assertIn("reset_quality_history_cache()", src)
        cap = src.index("await async_capture_quality_day(hass, entry_id)")
        guard = src.rfind("if ok and entry_id:", 0, cap)
        self.assertGreater(
            guard, 0, "must sit under the success guard, like the snapshot capture"
        )


if __name__ == "__main__":
    unittest.main()
