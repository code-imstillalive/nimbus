"""nimbus #582's same-day-in-progress fix, now one helper instead of four
copies (extracted under #485).

`_resolve_hour_to_period_index()` resolves "the next occurrence from now"
for each hour INDEPENDENTLY. Once `now` is past today's `earliest_hour`,
earliest rolls to TOMORROW while deadline stays today -- correct for a
genuine overnight window, wrong for a same-day window already in
progress. Mark Purcell's real repro on the first live morning of #534:
earliest=6, deadline=16, `now`=06:01, and the load was dropped for its
**entire active window** -- the opposite of intended.

## Why this file exists at all

The fix was applied by duplication, and the 09-09 worklog records the
cost already being paid once:

> While rebasing onto #582, found and fixed a real inconsistency:
> `apply_commanded_state_guard()`'s own duplicated period-index
> resolution **didn't inherit #582's same-day fix** ... flagged the drift
> risk between the two call sites as a candidate for a future
> shared-helper refactor.

That correction was itself applied by duplicating, and the copies reached
four -- two in `build_controllable_loads()`, two in
`apply_commanded_state_guard()`.

**Checked before extracting rather than assumed: all four were identical
in logic**, differing only in `ruff format` line wrapping at different
indentation depths. So the consolidation fixed no live divergence. What
it removes is the room for the next one, which has already happened once
and was caught by a rebase rather than by any test -- because a test can
only ever exercise the copy it happens to reach.

It also shrinks the question #485 asks. Moding `deadline`/`earliest`
hours per household mode multiplies the distinct hour pairs flowing
through this logic; "is the one helper right" is answerable in a way
"are the four copies still in agreement" is not.

## A fifth site shares the predicate and is deliberately left alone

`_build_daily_adequacy_windows()` evaluates the same
`earliest_today <= now <= deadline_today` test inside a per-day loop, as
one arm of a conditional that also handles `now > deadline_today`,
`today_done` and an already-met target. A superset, not a copy --
`TestTheFifthSiteIsNotACopy` pins that distinction so nobody folds it in
later on the strength of a grep.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import solver_writer


def _at(hour, minute=0):
    return datetime(2026, 9, 18, hour, minute, tzinfo=UTC)


def _earliest(now, earliest_hour, deadline_hour, earliest_period, deadline_period):
    return solver_writer._earliest_period_for_same_day_window(
        now=now,
        earliest_hour=earliest_hour,
        deadline_hour=deadline_hour,
        earliest_period=earliest_period,
        deadline_period=deadline_period,
    )


class TestMarksRealRepro(unittest.TestCase):
    """earliest=6, deadline=16, `now`=06:01 -- the #534 heat pump on its
    first live morning. earliest resolved to tomorrow (a large index)
    while deadline stayed today, so deadline < earliest and the load was
    dropped for its whole window."""

    def test_a_same_day_window_in_progress_opens_right_now(self):
        self.assertEqual(
            _earliest(_at(6, 1), 6.0, 16.0, earliest_period=96, deadline_period=40),
            0,
            "a window that opened earlier today and is still open must "
            "let the load draw now -- this is #582, and getting it wrong "
            "drops the load for its entire active window",
        )

    def test_just_before_the_window_opens_is_untouched(self):
        """05:59 against a 6am start: earliest has NOT rolled, the
        ordering is already correct, and nothing should move."""
        self.assertEqual(
            _earliest(_at(5, 59), 6.0, 16.0, earliest_period=4, deadline_period=40), 4
        )

    def test_after_the_window_closes_is_untouched(self):
        """17:00 against a 6-16 window: today's window is over, earliest
        correctly points at tomorrow, and the guard must not drag it back
        to 'now'."""
        self.assertEqual(
            _earliest(_at(17), 6.0, 16.0, earliest_period=52, deadline_period=88), 52
        )


class TestTheGenuineOvernightCaseSurvives(unittest.TestCase):
    """The case #582's fix had to avoid breaking. earliest=22,
    deadline=6, `now` between midnight and 6am: both resolve relative to
    `now` and the window really does run overnight."""

    def test_an_overnight_window_is_not_treated_as_same_day(self):
        now = _at(2)
        # earliest_today = 22:00 today, which is NOT <= now, so the
        # same-day branch must not fire.
        self.assertEqual(
            _earliest(now, 22.0, 6.0, earliest_period=80, deadline_period=16), 80
        )

    def test_an_overnight_window_before_midnight_is_untouched(self):
        self.assertEqual(
            _earliest(_at(23), 22.0, 6.0, earliest_period=4, deadline_period=28), 4
        )


class TestTheNoOpCases(unittest.TestCase):
    def test_already_correctly_ordered_is_returned_unchanged(self):
        """The guard only engages when deadline < earliest. An ordinary
        window must never be touched."""
        self.assertEqual(
            _earliest(_at(8), 6.0, 16.0, earliest_period=0, deadline_period=40), 0
        )

    def test_a_missing_earliest_hour_is_a_no_op(self):
        self.assertEqual(
            _earliest(_at(8), None, 16.0, earliest_period=96, deadline_period=40), 96
        )

    def test_a_missing_deadline_hour_is_a_no_op(self):
        self.assertEqual(
            _earliest(_at(8), 6.0, None, earliest_period=96, deadline_period=40), 96
        )

    def test_equal_periods_are_not_disturbed(self):
        """deadline == earliest is a one-period window, correctly
        ordered. Only a strict inversion is the #582 signature."""
        self.assertEqual(
            _earliest(_at(8), 6.0, 16.0, earliest_period=40, deadline_period=40), 40
        )


class TestTheBoundaries(unittest.TestCase):
    """The predicate is `earliest_today <= now <= deadline_today`, both
    inclusive. Worth pinning after #1104, where a boundary comparison in
    a different guard turned out to be the whole defect."""

    def test_exactly_at_the_opening_hour_counts_as_in_progress(self):
        self.assertEqual(
            _earliest(_at(6), 6.0, 16.0, earliest_period=96, deadline_period=40), 0
        )

    def test_exactly_at_the_deadline_hour_counts_as_in_progress(self):
        self.assertEqual(
            _earliest(_at(16), 6.0, 16.0, earliest_period=96, deadline_period=40), 0
        )

    def test_one_minute_past_the_deadline_does_not(self):
        self.assertEqual(
            _earliest(_at(16, 1), 6.0, 16.0, earliest_period=96, deadline_period=40),
            96,
        )


class TestTheCopiesAreGone(unittest.TestCase):
    """The point of the extraction. Source-checked because the defect
    class is duplication itself, which no behavioural test can see."""

    def test_the_helper_has_exactly_four_callers(self):
        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(
            text.count("_earliest_period_for_same_day_window"),
            5,
            "expected one definition and exactly four call sites (two in "
            "build_controllable_loads, two in apply_commanded_state_guard) "
            "-- a fifth copy of this logic reappearing inline is the exact "
            "drift #485 asked about, and it has already cost this project "
            "one real inconsistency",
        )

    def test_no_inline_copy_of_the_predicate_remains_in_those_functions(self):
        """`earliest_today` should now appear only inside the helper and
        inside `_build_daily_adequacy_windows()`, which is a superset
        rather than a copy."""
        import ast

        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name in (
                "_earliest_period_for_same_day_window",
                "_build_daily_adequacy_windows",
            ):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id == "earliest_today"
                ):
                    offenders.append(f"{fn.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "an inline copy of #582's same-day computation has reappeared "
            f"outside the helper: {offenders}. Call "
            "_earliest_period_for_same_day_window() instead -- four copies "
            "is how this started.",
        )


class TestTheFifthSiteIsNotACopy(unittest.TestCase):
    """`_build_daily_adequacy_windows()` shares the predicate but is a
    superset, and is deliberately excluded. Pinned so nobody folds it in
    later on the strength of a grep, and so that if it ever IS reduced to
    the same shape, this test says why that would now be safe.
    """

    def test_it_handles_cases_the_helper_does_not(self):

        source = solver_writer.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        start = text.index("def _build_daily_adequacy_windows(")
        end = text.index("def ", start + 10)
        body = text[start:end]
        for marker in ("today_done", "now > deadline_today", "day_target_kwh"):
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    "_build_daily_adequacy_windows() no longer handles the "
                    "cases that made it a superset rather than a copy of "
                    "the #582 guard. If it has genuinely been reduced to "
                    "the same shape, it can now call "
                    "_earliest_period_for_same_day_window() -- and should.",
                )


if __name__ == "__main__":
    unittest.main()
