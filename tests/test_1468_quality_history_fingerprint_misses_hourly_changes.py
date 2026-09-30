"""IV&V finding (nimbus #1468, sub-issue of #1467): `quality_history_store.py`'s
`fingerprint()` does not cover the per-hour detail the file exists to archive.

`_HOURLY_KEYS` (`hourly_regret`, `soc_discrepancy_hourly`, `j_ref_hourly`,
`j_ach_hourly`, `j_star_hourly`) is the module's own stated reason for
existing -- its docstring calls it "the per-hour detail that ages out and
cannot be rebuilt". `fingerprint()` only looks at `epr`/`j_ach`/
`regret_dollars`/`real_p2p_settlement_status`/`provisional`. None of the
`_HOURLY_KEYS`, and none of the `soc_discrepancy_*`/`epr_reliable`/
`epr_reason` scalars, are in it.

This is not hypothetical: nimbus #1438 (landed a few hours earlier in the
same commit range, `952146d`) fixed a real case where `hourly_regret`'s sum
and `regret_dollars` legitimately disagree by design (`regret_dollars`
includes the one-time `salvage_value` term the hourly buckets deliberately
exclude). A correction that redistributes `hourly_regret` across hours, or
fixes a `soc_discrepancy_hourly` reconstruction (this repo has done exactly
that more than once -- #538, #533, #949), can leave the five fingerprinted
scalars completely unchanged while the archived per-hour detail is now
stale -- and `async_capture_quality_day()` (called every solve cycle) will
never re-write it.
"""

from __future__ import annotations

import asyncio
import copy
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


def _report(day="2026-09-28", hourly_regret=None):
    return {
        "latest_date": day,
        "epr": 0.73,
        "epr_pct": 73.0,
        "j_ref": 2.8034,
        "j_ach": -3.8656,
        "j_star": -4.4938,
        "j_star_evaluator": -6.3325,
        "regret_dollars": 2.4669,
        "real_p2p_settlement_status": "applied",
        "nimbus_version": "0.94.430",
        "hourly_regret": hourly_regret or {"15": 1.413, "14": 0.9287},
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
        "history": {day: {"epr": 0.73, "j_ach": -3.8656, "v": "0.94.430"}},
        "friendly_name": "not archived",
    }


class _State:
    def __init__(self, attributes):
        self.attributes = attributes


class _Hass:
    def __init__(self, attributes):
        self._state = _State(attributes)

    @property
    def states(self):
        hass = self

        class _S:
            def get(self, entity_id):
                return hass._state if entity_id == qhs._SOURCE_ENTITY else None

        return _S()


class _FakeStore:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.saves: list[dict] = []

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, data):
        self.saves.append(data)
        self.data = dict(data)


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


class TestFingerprintCoversWhatItArchives(_Base):
    """Pure, fast: fingerprint() must change when the archived hourly detail
    changes, even with every fingerprinted scalar held identical."""

    def test_a_changed_hourly_regret_breakdown_changes_the_fingerprint(self):
        day1, rec1 = qhs.build_quality_capture(
            _report(hourly_regret={"15": 1.413, "14": 0.9287}), captured_at="t1"
        )
        day2, rec2 = qhs.build_quality_capture(
            # Same day, same epr/j_ach/regret_dollars/status/provisional --
            # only the HOURLY breakdown moved (a real, documented
            # possibility per #1438: hourly buckets and the day total are
            # computed independently and are allowed to disagree).
            _report(hourly_regret={"15": 0.0, "14": 2.3417}),
            captured_at="t2",
        )
        self.assertEqual(day1, day2)
        self.assertNotEqual(
            rec1["hourly_regret"],
            rec2["hourly_regret"],
            "the fixture itself must actually differ, or this test proves nothing",
        )
        self.assertNotEqual(
            qhs.fingerprint(rec1),
            qhs.fingerprint(rec2),
            "fingerprint() is blind to hourly_regret -- two records with "
            "genuinely different archived per-hour detail hash identically, "
            "nimbus #1468",
        )


class TestCaptureReArchivesOnHourlyChange(_Base):
    """End-to-end through the real async entry point: a day already archived
    must be re-written once its hourly detail changes, even though every
    fingerprinted scalar stays the same."""

    def test_a_hourly_only_change_triggers_a_rewrite(self):
        store = _FakeStore()
        first = _Hass(_report(hourly_regret={"15": 1.413, "14": 0.9287}))
        self.assertTrue(_run(first, store))
        archived_first = copy.deepcopy(store.data["2026-09-28"]["hourly_regret"])

        second = _Hass(_report(hourly_regret={"15": 0.0, "14": 2.3417}))
        wrote_again = _run(second, store)

        self.assertTrue(
            wrote_again,
            "async_capture_quality_day() returned False -- it believes "
            "nothing changed because fingerprint() never looked at "
            "hourly_regret, so the stale per-hour detail from the first "
            "capture is what a disputed-day rescore would read back, "
            "nimbus #1468",
        )
        self.assertNotEqual(
            store.data["2026-09-28"]["hourly_regret"],
            archived_first,
            "the archive still holds the FIRST capture's hourly_regret",
        )


class TestFingerprintCoversArchivedScalarsToo(_Base):
    """#1468 named these as well: archived scalars outside the old five."""

    def test_every_archived_field_moves_the_fingerprint(self):
        _, base = qhs.build_quality_capture(_report(), captured_at="t1")
        for key, value in (
            ("soc_discrepancy_max_pct", 25.1),
            ("epr_reliable", False),
            ("epr_reason", "soc_disagreement"),
            ("soc_discrepancy_hourly", []),
            ("j_star_hourly", []),
        ):
            changed = dict(base, **{key: value})
            self.assertNotEqual(
                qhs.fingerprint(base), qhs.fingerprint(changed), f"blind to {key}"
            )

    def test_captured_at_alone_still_does_not_rewrite(self):
        store = _FakeStore()
        self.assertTrue(_run(_Hass(_report()), store))
        self.assertFalse(_run(_Hass(_report()), store))
        self.assertEqual(len(store.saves), 1)


if __name__ == "__main__":
    unittest.main()
