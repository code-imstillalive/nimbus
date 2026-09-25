"""nimbus #1219 and #1220: the two fields that say WHEN and BY WHAT a
scored day was produced, both going stale on a republish.

These are one defect with two faces, and the face they share is the
reason neither was caught by the guard built to catch exactly this.

**#1219 — the version stamp advances without a rescore.**
`publish_daily_quality_report()`'s idempotency fast path re-pushes the
already-published attributes to keep the freshness stamp alive
(#289/#292) and seeds the history table while it is there (#994). It
passes the published attributes back in as `day_entry`. Nothing is
recomputed, but `_carry_forward_quality_history()` stamped the row with
the running release anyway.

Measured on the reference household, 2026-09-25: the 2026-09-24 row moved
`v: 0.94.413` -> `v: 0.94.417` across a restart while `generated_at`
stayed `06:00:00` and every figure was byte-identical (`epr 21.11`,
`j_ach -3.0503`, `j_star -20.9741`). The row asserted that v0.94.417
produced numbers v0.94.413 produced.

**#1220 — a rescore republishes under a stale timestamp.**
`rescore_quality_history()` merges the fresh report with
`attrs.update(latest_entry)`, but `generated_at` is not a field of the
report -- the publisher adds it -- so `update()` cannot supply it.

Measured the same day: a rescore moved `epr` 21.11 -> 68.85, `j_ach`
-3.0503 -> -14.6231 and `achieved_soc_max_pct` 125.4833 -> 92.1833 while
`generated_at` stayed at `2026-09-25T06:00:00+10:00`.

Not merely cosmetic: `_keep_published_quality_score()` (#1082) decides
whether a provisional day is re-scored from
`age = now - parse_iso(generated_at)`, so a stale value drives the retry
cadence off a superseded computation.

**Why one guard missed both.** #1167 shipped a sweep that drives a
rescore and asserts every field of the recomputed report reaches the
published attributes -- *discovering* the report's fields rather than
listing them, precisely so a new field is covered without anyone
remembering. It cannot catch these two, by construction: neither
`generated_at` nor the version stamp is a field of the report. The
publisher adds them. The enumeration problem was solved for the report's
own fields and left unsolved for the publisher's.

That is the observation these tests exist to pin, so the next
publisher-owned field does not make it three.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import _solver_path  # noqa: F401
import solver_writer

AEST = timezone(timedelta(hours=10))


def _entry(**over):
    e = {
        "epr": 0.9,
        "epr_pct": 90.0,
        "j_ref": 5.0,
        "j_ach": -20.0,
        "j_star": -21.0,
        "regret_dollars": 1.0,
        "real_p2p_settlement_status": "applied",
    }
    e.update(over)
    return e


def _now(day=25, hour=11):
    return datetime(2026, 9, day, hour, 0, tzinfo=AEST)


class TestTheVersionStampFollowsTheComputation(unittest.TestCase):
    """#1219 -- a row must name the release that SCORED it."""

    KEY = "2026-09-24"

    def _prior(self, stamped="0.94.413"):
        row = {"epr": 0.2111, "j_ref": 2.58, "j_ach": -3.05}
        if stamped is not None:
            row[solver_writer._QUALITY_HISTORY_VERSION_FIELD] = stamped
        return {"history": {self.KEY: row}}

    def test_a_fresh_computation_stamps_the_running_release(self):
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.418"
        ):
            h = solver_writer._carry_forward_quality_history(
                self._prior(), self.KEY, _entry()
            )
        self.assertEqual(
            h[self.KEY][solver_writer._QUALITY_HISTORY_VERSION_FIELD], "0.94.418"
        )

    def test_a_repush_preserves_the_stamp_it_found(self):
        """The incident: restart on a new release, nothing recomputed."""
        prior = self._prior("0.94.413")
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.417"
        ):
            h = solver_writer._carry_forward_quality_history(
                prior, self.KEY, prior["history"][self.KEY], freshly_computed=False
            )
        self.assertEqual(
            h[self.KEY][solver_writer._QUALITY_HISTORY_VERSION_FIELD],
            "0.94.413",
            "a re-push recomputes nothing, so it must not claim the running "
            "release produced these figures",
        )

    def test_a_repush_of_an_unstamped_row_leaves_it_unstamped(self):
        """#1120 deliberately wanted absence to keep meaning 'written
        before this existed'. A re-push must not back-date it."""
        prior = self._prior(stamped=None)
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.418"
        ):
            h = solver_writer._carry_forward_quality_history(
                prior, self.KEY, prior["history"][self.KEY], freshly_computed=False
            )
        self.assertNotIn(solver_writer._QUALITY_HISTORY_VERSION_FIELD, h[self.KEY])

    def test_prior_rows_are_never_touched_either_way(self):
        """The control -- #1120's own rule, unchanged by this fix."""
        prior = {
            "history": {
                "2026-09-22": {"epr": 0.877, "v": "0.94.413"},
                self.KEY: {"epr": 0.2111, "v": "0.94.413"},
            }
        }
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.418"
        ):
            h = solver_writer._carry_forward_quality_history(prior, self.KEY, _entry())
        self.assertEqual(h["2026-09-22"]["v"], "0.94.413")


class TestARescoreRefreshesTheTimestamp(unittest.TestCase):
    """#1220 -- generated_at must describe the figures beside it."""

    def _run_rescore(self, now):
        published = {
            "attributes": {
                "latest_date": "2026-09-24",
                "generated_at": "2026-09-25T06:00:00+10:00",
                "history": {"2026-09-24": {"epr": 0.2111}},
            },
            "state": "21.11",
        }
        posted: dict = {}

        def post(entity_id, state, attrs):
            posted["state"] = state
            posted["attrs"] = attrs

        with (
            mock.patch.object(solver_writer, "ha_get", return_value=published),
            mock.patch.object(
                solver_writer,
                "_compute_report_for_window",
                side_effect=lambda cfg, s, e, allow_partial: _entry(
                    epr=0.6885, epr_pct=68.85, j_ach=-14.6231
                ),
            ),
            mock.patch.object(solver_writer, "ha_post_state", side_effect=post),
            mock.patch.object(
                solver_writer, "_nimbus_version", return_value="0.94.418"
            ),
        ):
            solver_writer.rescore_quality_history({}, now, 1)
        return posted

    def test_generated_at_moves_with_the_figures(self):
        now = _now()
        posted = self._run_rescore(now)
        self.assertEqual(posted["attrs"]["generated_at"], now.isoformat())
        self.assertNotEqual(
            posted["attrs"]["generated_at"],
            "2026-09-25T06:00:00+10:00",
            "the timestamp must not still describe the publish these "
            "figures just replaced",
        )

    def test_the_figures_really_did_change(self):
        """Control: if the rescore published nothing new, a moving
        timestamp would prove nothing."""
        posted = self._run_rescore(_now())
        self.assertEqual(posted["state"], 68.85)
        self.assertAlmostEqual(posted["attrs"]["j_ach"], -14.6231)

    def test_the_version_stamp_still_moves_too(self):
        """v0.94.402's fix must survive this one -- both publisher-owned
        fields are refreshed, not one at the other's expense."""
        posted = self._run_rescore(_now())
        self.assertEqual(posted["attrs"]["nimbus_version"], "0.94.418")


class TestTheSharedObservation(unittest.TestCase):
    def test_neither_field_is_part_of_the_report(self):
        """The reason #1167's field-discovering guard cannot catch these.

        If either ever becomes a report field, this test fails and the
        guard takes over -- which is the outcome to want, not a problem.
        """
        import inspect

        src = inspect.getsource(solver_writer._compute_report_for_window)
        self.assertNotIn(
            '"generated_at"',
            src,
            "generated_at is publisher-owned; if the report starts carrying "
            "it, #1167's sweep covers it and this guard is redundant",
        )
        self.assertNotIn('"nimbus_version"', src)


if __name__ == "__main__":
    unittest.main()
