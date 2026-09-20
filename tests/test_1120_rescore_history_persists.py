"""nimbus issue #1120, part 3 -- nimbus_load.rescore_history, the
write-back half of the quality scorer.

A day is scored ONCE and frozen, so every scoring-formula change splits
the history table into two incomparable halves. compute_quality_report()
already scores an arbitrary window correctly -- it returns the answer and
writes nothing back, so an operator who knows exactly which row is stale
still has no way to fix it. rescore_quality_history() is that missing
path.

Parts 1 and 2 of #1120 shipped earlier (the per-row version stamp in
v0.94.392, and the trend card's axis clipping). This covers part 3, which
that issue's own last comment calls the only substantive item left.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 9, 20, 6, 30, tzinfo=BRISBANE)


def _entry(epr):
    """A day_entry shaped like _compute_report_for_window() returns."""
    return {
        "epr": epr,
        "epr_pct": round(epr * 100.0, 2),
        "j_ref": 6.0,
        "j_ach": -24.0,
        "j_star": -25.0,
        "regret_dollars": 1.0,
    }


def _existing(history=None, latest_date=None, state="94.81"):
    attrs = {"latest_date": latest_date, "history": dict(history or {})}
    return {"state": state, "attributes": attrs}


def _run(days, existing, side_effect, version="0.94.396"):
    """Drive rescore_quality_history() with every real dependency stubbed."""
    posted = {}

    def _post(entity_id, state, attributes):
        posted["entity_id"] = entity_id
        posted["state"] = state
        posted["attributes"] = attributes

    with (
        patch.object(solver_writer, "ha_get", return_value=existing),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        patch.object(solver_writer, "_nimbus_version", return_value=version),
        patch.object(
            solver_writer, "_compute_report_for_window", side_effect=side_effect
        ),
    ):
        result = solver_writer.rescore_quality_history({}, NOW, days)
    return result, posted


class TestTheDayCountIsBounded(unittest.TestCase):
    def test_zero_or_negative_days_is_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                solver_writer.rescore_quality_history({}, NOW, bad)

    def test_more_than_the_cap_is_rejected(self):
        # Each day is a full oracle MILP, so an accidental "rescore
        # everything" must cost an error rather than an hour.
        with self.assertRaises(ValueError):
            solver_writer.rescore_quality_history(
                {}, NOW, solver_writer._RESCORE_MAX_DAYS + 1
            )

    def test_the_cap_itself_is_allowed(self):
        result, _ = _run(
            solver_writer._RESCORE_MAX_DAYS, _existing(), lambda *a, **k: None
        )
        self.assertEqual(result["days_requested"], solver_writer._RESCORE_MAX_DAYS)


class TestAStaleRowIsReplacedAndStamped(unittest.TestCase):
    def test_the_row_is_rewritten_with_the_new_figures(self):
        stale = {"2026-09-19": {"epr": 1.064, "j_star": -6.84, "v": "0.94.375"}}
        result, posted = _run(1, _existing(stale), lambda *a, **k: _entry(0.9481))

        row = posted["attributes"]["history"]["2026-09-19"]
        self.assertAlmostEqual(row["epr"], 0.9481)
        self.assertAlmostEqual(row["j_star"], -25.0)
        self.assertEqual(result["rescored_count"], 1)
        self.assertEqual(result["rescored"][0]["before"]["epr"], 1.064)
        self.assertAlmostEqual(result["rescored"][0]["after"]["epr"], 0.9481)

    def test_the_rewritten_row_carries_the_release_that_rescored_it(self):
        # A table rescored halfway must still say which half is which --
        # that is part 1 of this issue, and a rescore must not defeat it.
        stale = {"2026-09-19": {"epr": 1.064, "v": "0.94.375"}}
        _, posted = _run(1, _existing(stale), lambda *a, **k: _entry(0.9481))
        self.assertEqual(posted["attributes"]["history"]["2026-09-19"]["v"], "0.94.396")

    def test_days_outside_the_window_are_left_exactly_as_found(self):
        untouched = {"epr": 0.5, "v": "0.90.0"}
        hist = {"2026-08-01": dict(untouched), "2026-09-19": {"epr": 1.064}}
        _, posted = _run(1, _existing(hist), lambda *a, **k: _entry(0.9481))
        self.assertEqual(posted["attributes"]["history"]["2026-08-01"], untouched)


class TestAnInstallThatHasNeverBeenScored(unittest.TestCase):
    """A fresh install has no `history` key on the sensor at all -- not an
    empty dict, absent. Rescoring there must build the table rather than
    fail on a missing key, because "I just set this up and my table is
    empty" is exactly when someone reaches for a backfill."""

    def test_a_missing_history_key_is_built_rather_than_crashing(self):
        bare = {"state": "unknown", "attributes": {"latest_date": None}}
        result, posted = _run(2, bare, lambda *a, **k: _entry(0.9))

        self.assertEqual(result["rescored_count"], 2)
        self.assertEqual(
            sorted(posted["attributes"]["history"]),
            ["2026-09-18", "2026-09-19"],
        )

    def test_a_sensor_with_no_attributes_at_all_is_survivable(self):
        result, posted = _run(1, {"state": None}, lambda *a, **k: _entry(0.9))
        self.assertEqual(result["rescored_count"], 1)
        self.assertIn("2026-09-19", posted["attributes"]["history"])


class TestOneBadDayDoesNotCostTheRest(unittest.TestCase):
    def test_an_unscoreable_day_is_skipped_with_a_reason(self):
        def _side(cfg, start, end, allow_partial):
            return None if start.date().isoformat() == "2026-09-18" else _entry(0.9)

        result, posted = _run(3, _existing(), _side)

        self.assertEqual(result["rescored_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["date"], "2026-09-18")
        self.assertIn("no usable real history", result["skipped"][0]["reason"])
        # the two that DID score were still written
        self.assertIn("2026-09-19", posted["attributes"]["history"])
        self.assertIn("2026-09-17", posted["attributes"]["history"])

    def test_a_raising_day_is_skipped_rather_than_aborting_the_run(self):
        def _side(cfg, start, end, allow_partial):
            if start.date().isoformat() == "2026-09-18":
                raise RuntimeError("oracle infeasible")
            return _entry(0.9)

        result, posted = _run(3, _existing(), _side)

        self.assertEqual(result["rescored_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("RuntimeError", result["skipped"][0]["reason"])
        self.assertIn("oracle infeasible", result["skipped"][0]["reason"])
        self.assertTrue(posted, "the days that scored must still be published")

    def test_nothing_is_published_when_no_day_could_be_scored(self):
        result, posted = _run(3, _existing(), lambda *a, **k: None)
        self.assertEqual(result["rescored_count"], 0)
        self.assertFalse(result["published"])
        self.assertEqual(posted, {}, "must not publish an unchanged table")


class TestTheHeadlineAndTheTableStayInAgreement(unittest.TestCase):
    """The Yesterday card reads the sensor's top-level attributes while
    the trend card reads history. Correcting one and not the other leaves
    them disagreeing -- which is the defect #1120 is about, not a fix for
    it."""

    def test_rescoring_the_published_day_moves_the_headline_and_the_state(self):
        existing = _existing(
            {"2026-09-19": {"epr": 1.064}}, latest_date="2026-09-19", state="106.43"
        )
        _, posted = _run(1, existing, lambda *a, **k: _entry(0.9481))

        self.assertEqual(posted["state"], 94.81, "state channel is epr_pct")
        self.assertAlmostEqual(posted["attributes"]["epr"], 0.9481)
        self.assertAlmostEqual(posted["attributes"]["j_star"], -25.0)
        self.assertAlmostEqual(posted["attributes"]["regret_dollars"], 1.0)

    def test_rescoring_an_older_day_leaves_the_headline_alone(self):
        existing = _existing(
            {"2026-09-17": {"epr": 0.2}}, latest_date="2026-09-19", state="106.43"
        )
        existing["attributes"]["epr"] = 1.064

        def _side(cfg, start, end, allow_partial):
            return _entry(0.5) if start.date().isoformat() == "2026-09-17" else None

        _, posted = _run(3, existing, _side)

        self.assertEqual(posted["state"], "106.43", "headline must not move")
        self.assertAlmostEqual(posted["attributes"]["epr"], 1.064)
        self.assertAlmostEqual(
            posted["attributes"]["history"]["2026-09-17"]["epr"], 0.5
        )


class TestTheWindowMatchesTheDailyScorer(unittest.TestCase):
    """A rescored row must be directly comparable to one written the
    ordinary way, which means the same local-midnight boundaries and the
    same allow_partial=False."""

    def test_each_day_is_scored_local_midnight_to_local_midnight(self):
        seen = []

        def _side(cfg, start, end, allow_partial):
            seen.append((start, end, allow_partial))
            return _entry(0.9)

        _run(2, _existing(), _side)

        self.assertEqual(len(seen), 2)
        for start, end, allow_partial in seen:
            self.assertEqual(start.tzinfo, BRISBANE)
            self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
            self.assertEqual(end - start, timedelta(days=1))
            self.assertFalse(
                allow_partial, "a whole-day row must never be a partial score"
            )

    def test_it_counts_back_from_yesterday_never_including_today(self):
        seen = []

        def _side(cfg, start, end, allow_partial):
            seen.append(start.date().isoformat())
            return _entry(0.9)

        _run(3, _existing(), _side)
        self.assertEqual(seen, ["2026-09-19", "2026-09-18", "2026-09-17"])
        self.assertNotIn("2026-09-20", seen, "today is not a complete day")


if __name__ == "__main__":
    unittest.main()
