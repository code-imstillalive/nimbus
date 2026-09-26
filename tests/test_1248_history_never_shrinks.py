"""nimbus issue #1248: a degraded read must not permanently truncate the
published quality history.

## What happened, measured

On production 2026-09-26 the published `history` went from **10 rows to 1**
with **no restart** -- the error log spans 25 Sep 11:20 -> 26 Sep 06:15 and
contains exactly one `Starting Home Assistant`, hours before the first read.
The nine lost days (16-24 Sep) were unrecoverable from the attribute.

It happened inside a live #1217 reproduction:

    06:00:59  quality skip: (solar=14398, load=4183, battery=0 rows)
    06:05:06  every flattened quality child -> unavailable (>300s no plan)
    06:05:43  quality skip: (solar=0, load=0, battery=0 rows)
    06:06:39  solver: cycle took 147.7s (> 120s threshold)

The report that ended up holding one row was written at **06:06**, inside that
~2-minute window where the whole quality surface read `unavailable`.

## The ratchet

`publish_daily_quality_report()` reads the currently-published attributes and
defaults them to `{}` when the read fails. Its own comment states the flawed
assumption outright -- *"a first-ever publish, **or** an unreachable read"* --
treating those as the same thing. They are not: an unreachable read is not an
empty history, it is an **unknown** history.

`_carry_forward_quality_history()` then begins from that `{}`, writes only the
day it just computed, and `ha_post_state` replaces attributes wholesale. **The
next cycle carries forward from the already-truncated attribute**, so one
degraded read discards every older row permanently.

## The fix, and why it is not "skip the publish"

Refusing to publish on a failed read is the obvious move and it is wrong: a
genuine first-ever publish on a fresh install *also* fails that read (404 on an
entity that does not exist yet), so skipping would mean a new install could
never write its first row. The two cases are indistinguishable at the caller.

They are distinguishable one layer down. `_LAST_KNOWN_QUALITY_HISTORY` records
what this process last published, so an empty prior can be classified:

* cache empty -> genuinely nothing known -> a first publish, allowed;
* cache populated -> this process has already published rows, so it **cannot**
  be a first publish -> degraded read, recover the cache.

Process-lifetime is deliberate and sufficient. The measured incident had no
restart, so this closes it exactly; and across a real restart HA's own state
restoration repopulates the attribute, so starting empty on a fresh process is
correct rather than lossy. A durable Store would add a second system of record
for no gain against the failure actually observed.

## What was tried and REMOVED, so it is not re-proposed

A "refuse to publish a shorter table than the prior one" shrink guard, comparing
`len(history)` against the prior row count. It cannot fire for this defect:
`history` starts as a copy of *every* sane prior row and only ever adds one key,
so it is structurally never shorter than its own prior. The only way to trip it
was a malformed prior entry -- a case it then "recovered" to the identical
value while logging a warning blaming a recorder stall. A guard documented to
catch a case it cannot catch is worse than no guard, because the comment is
read as evidence the case is handled.

## What these tests pin

* The production shape: a full table, then an empty read, recovers all ten rows.
* **The freshly computed row still wins for its own day**, on the recovery path
  too -- otherwise a rescore could never correct a row.
* A genuine first publish is still allowed, and the cache does not leak across
  tests to make one impossible.
* The recovery is **loud**. A silent recovery would hide the degraded read that
  triggered it, and that read is the thing worth knowing about.
* The `_QUALITY_HISTORY_MAX_DAYS` trim still applies after recovery.
* The cache tracks the trimmed, published table -- not the pre-trim one -- or it
  would reinstate rows the cap had deliberately dropped.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer  # noqa: E402


def _row(epr: float) -> dict:
    return {
        "epr": epr,
        "j_ref": 1.0,
        "j_ach": -1.0,
        "j_star": -2.0,
        "regret_dollars": 1.0,
    }


def _carry(prior_history, day_key: str, epr: float):
    """Call the real carry-forward with a prior attribute payload.

    `prior_history=None` is the degraded read as it actually presents: the
    attributes dict has no `history` key at all.
    """
    prior_attrs = {} if prior_history is None else {"history": prior_history}
    return solver_writer._carry_forward_quality_history(
        prior_attrs=prior_attrs,
        day_key=day_key,
        day_entry=dict(_row(epr)),
    )


class _IsolatedCache(unittest.TestCase):
    """The fix is module-level state, so every test starts from a cold process.

    Without this, test order decides outcomes -- which would make the
    first-publish tests below pass or fail depending on what ran before them.
    """

    def setUp(self):
        solver_writer._LAST_KNOWN_QUALITY_HISTORY = {}
        self.addCleanup(setattr, solver_writer, "_LAST_KNOWN_QUALITY_HISTORY", {})


class TestTheProductionShapeIsRecovered(_IsolatedCache):
    """10 rows, then a degraded read -- the exact loss measured 2026-09-26."""

    def setUp(self):
        super().setUp()
        self.prior = {f"2026-09-{d:02d}": _row(90.0 + d) for d in range(16, 26)}
        self.assertEqual(len(self.prior), 10)

    def test_a_degraded_read_does_not_discard_the_other_nine_days(self):
        # Cycle 1: a healthy read, ten rows. This is what the process had been
        # re-publishing every minute all day.
        first = _carry(self.prior, "2026-09-25", 68.9)
        self.assertEqual(len(first), 10)

        # Cycle 2 at 06:06: the sensor reads `unavailable`, so the attributes
        # come back with no history at all. Before the fix this published one
        # row and the next cycle inherited it.
        out = _carry(None, "2026-09-26", 55.0)
        self.assertEqual(
            len(out),
            11,
            "the ten known rows must survive a degraded read; losing them is "
            "unrecoverable from the attribute",
        )
        for d in range(16, 26):
            self.assertIn(f"2026-09-{d:02d}", out)
        self.assertIn("2026-09-26", out, "and today's real score still lands")

    def test_the_ratchet_cannot_turn_even_over_many_degraded_cycles(self):
        """The defect was not one bad write -- it was that the next cycle
        carried forward from the bad write. Ten consecutive degraded reads must
        leave the table intact, not erode it one row at a time."""
        _carry(self.prior, "2026-09-25", 68.9)
        out = None
        for _ in range(10):
            out = _carry(None, "2026-09-25", 68.9)
        self.assertEqual(len(out), 10)
        for d in range(16, 26):
            self.assertIn(f"2026-09-{d:02d}", out)

    def test_the_freshly_computed_row_still_wins_for_its_own_day(self):
        """Recovery must not resurrect a stale version of today, or a rescore
        could never correct a row -- which is the whole point of
        rescore_history, and of #1082's re-score-on-settlement path that
        produced 68.85% from a provisional 21.11%."""
        _carry(self.prior, "2026-09-25", 21.11)
        out = _carry(None, "2026-09-25", 68.9)
        self.assertEqual(
            out["2026-09-25"]["epr"],
            68.9,
            "today's row must be the freshly computed one, not the cached one",
        )
        self.assertEqual(
            out["2026-09-16"]["epr"], 106.0, "an untouched older row is unchanged"
        )

    def test_the_recovery_is_logged(self):
        """A silent recovery would hide the degraded read that triggered it,
        and that read is the thing worth knowing about -- it is a #1217
        symptom."""
        _carry(self.prior, "2026-09-25", 68.9)
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as cm:
            _carry(None, "2026-09-25", 68.9)
        joined = "\n".join(cm.output)
        self.assertIn("#1248", joined)
        self.assertIn("degraded read", joined)

    def test_a_healthy_read_logs_no_warning(self):
        """The warning must mean something. If it fired on the normal path it
        would be noise, and #1217's own logs are already noisy enough that a
        real signal has to stand out."""
        _carry(self.prior, "2026-09-25", 68.9)
        with self.assertNoLogs(solver_writer._LOGGER, level="WARNING"):
            _carry(self.prior, "2026-09-26", 55.0)


class TestAFirstPublishIsStillPossible(_IsolatedCache):
    """The reason the fix is not "skip the publish when the read fails"."""

    def test_a_fresh_install_writes_its_first_row(self):
        out = _carry(None, "2026-09-26", 55.0)
        self.assertEqual(len(out), 1, "a new install must be able to write row one")
        self.assertIn("2026-09-26", out)

    def test_an_empty_dict_prior_is_also_a_first_publish(self):
        out = _carry({}, "2026-09-26", 55.0)
        self.assertEqual(len(out), 1)

    def test_a_first_publish_logs_no_warning(self):
        """A brand-new install must not greet its owner with a data-loss
        warning on day one."""
        with self.assertNoLogs(solver_writer._LOGGER, level="WARNING"):
            _carry(None, "2026-09-26", 55.0)


class TestTheNormalPathsAreUntouched(_IsolatedCache):
    def test_growing_by_one_day_is_allowed(self):
        prior = {"2026-09-24": _row(87.3), "2026-09-25": _row(68.9)}
        out = _carry(prior, "2026-09-26", 55.0)
        self.assertEqual(len(out), 3)
        self.assertEqual(out["2026-09-26"]["epr"], 55.0)

    def test_rewriting_an_existing_day_keeps_the_same_size(self):
        prior = {"2026-09-24": _row(87.3), "2026-09-25": _row(21.11)}
        out = _carry(prior, "2026-09-25", 68.9)
        self.assertEqual(len(out), 2)
        self.assertEqual(
            out["2026-09-25"]["epr"], 68.9, "a rescore must be able to correct a row"
        )

    def test_a_malformed_prior_entry_does_not_block_the_write(self):
        """The function is already defensive about shape, and the fix must not
        turn a bad entry into a refusal to publish -- nor into a spurious
        degraded-read warning, which is what the removed shrink guard did."""
        prior = {"2026-09-24": _row(87.3), "bad": "not a dict", 42: {"epr": 1}}
        with self.assertNoLogs(solver_writer._LOGGER, level="WARNING"):
            out = _carry(prior, "2026-09-25", 68.9)
        self.assertIn("2026-09-25", out)
        self.assertIn("2026-09-24", out)
        self.assertNotIn("bad", out)


class TestTheCapStillApplies(_IsolatedCache):
    """Recovery must not let the table grow without limit."""

    def _at_cap(self) -> dict:
        cap = solver_writer._QUALITY_HISTORY_MAX_DAYS
        # Real ISO dates, so the lexicographic oldest-first trim is exercised
        # as it runs in production rather than on synthetic keys.
        start = date(2026, 1, 1)
        return {
            (start + timedelta(days=i)).isoformat(): _row(float(i))
            for i in range(cap)
        }

    def test_the_max_days_trim_is_not_defeated(self):
        cap = solver_writer._QUALITY_HISTORY_MAX_DAYS
        prior = self._at_cap()
        self.assertEqual(len(prior), cap)
        out = _carry(prior, "2026-12-31", 99.0)
        self.assertEqual(len(out), cap, f"history must not exceed the {cap}-day cap")
        self.assertIn("2026-12-31", out, "the new row must be the one kept")
        self.assertNotIn("2026-01-01", out, "the oldest row must be the one dropped")

    def test_the_cache_holds_the_trimmed_table_not_the_pre_trim_one(self):
        """If the cache were captured before the trim, a later degraded read
        would reinstate rows the cap had deliberately dropped -- the table
        would creep back over the cap every time the recorder stalled."""
        cap = solver_writer._QUALITY_HISTORY_MAX_DAYS
        _carry(self._at_cap(), "2026-12-31", 99.0)
        self.assertEqual(len(solver_writer._LAST_KNOWN_QUALITY_HISTORY), cap)
        self.assertNotIn("2026-01-01", solver_writer._LAST_KNOWN_QUALITY_HISTORY)
        out = _carry(None, "2027-01-01", 88.0)
        self.assertEqual(len(out), cap, "still capped after a degraded read")

    def test_the_cache_is_a_copy_not_a_live_alias(self):
        """A caller mutating the returned dict must not silently rewrite this
        process's idea of what was published."""
        out = _carry({"2026-09-24": _row(87.3)}, "2026-09-25", 68.9)
        out.clear()
        self.assertEqual(
            len(solver_writer._LAST_KNOWN_QUALITY_HISTORY),
            2,
            "the cache must be insulated from mutation of the returned table",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
