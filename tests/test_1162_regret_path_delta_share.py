"""nimbus #1162 (ask 3): say how much of the published regret is a
pricing-path disagreement rather than a dispatch difference.

## The identity, because it is not obvious from the field names

`j_star_path_delta` is exactly the amount the regret moved by repricing
the oracle's own plan through the evaluator instead of reading the LP's
objective::

    regret_evaluator - regret_raw
      = (j_ach - j_star_evaluator) - (j_ach - j_star)
      = j_star - j_star_evaluator
      = j_star_path_delta

So the delta is not merely *related* to the regret -- it is the portion
of it that comes from which oracle price you use.

## Why it needs publishing

Measured on the reference household, 19 Sep 2026::

    j_ach              -2.3078
    j_star             -3.3304      (the LP's own objective)
    j_star_evaluator   -5.9563      (the same plan, repriced)
    j_star_path_delta   2.6259
    regret_dollars      3.6485

Regret computed against the raw objective is $1.0226. Published regret
is $3.6485. **72% of the headline is the two paths disagreeing about the
oracle's own plan**, not the household having dispatched differently. A
household reading "$3.65 of regret" would go looking for a dispatch
mistake that was mostly not there.

That is the confident-wrong-number failure this project keeps paying
for. #1073 already fixed its twin on the energy-balance side, and the
choice made there is the one made here: **keep the figure, attach the
caveat**. The numbers still bound the answer and a reader who
understands the caveat can use them.

## Scope, stated plainly

This publishes a measurement. It does NOT change `regret_dollars`,
`epr`, or any reliability flag, and it deliberately does not introduce a
threshold above which the regret is declared untrustworthy -- picking
that number needs more than one install's worth of days, and a guard
calibrated on one day is exactly what #1057 warned about.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer

_share = solver_writer._regret_path_delta_share


class TestTheMeasuredDay(unittest.TestCase):
    """The reference household's own 19 Sep figures."""

    def test_it_reports_the_real_share(self):
        self.assertAlmostEqual(_share(3.6485, 2.6259), 0.7197, places=3)

    def test_the_identity_the_field_rests_on(self):
        """If this breaks, the field is measuring something else. Regret
        against the raw objective plus the delta must equal the published
        regret."""
        j_ach, j_star, j_star_eval = -2.3078, -3.3304, -5.9563
        regret_raw = j_ach - j_star
        regret_published = j_ach - j_star_eval
        path_delta = j_star - j_star_eval
        self.assertAlmostEqual(regret_raw + path_delta, regret_published, places=4)
        self.assertAlmostEqual(path_delta, 2.6259, places=4)

    def test_a_healthy_day_reports_nothing(self):
        """16 Sep on the same install: the paths agreed exactly, so the
        published regret is entirely a dispatch difference."""
        self.assertEqual(_share(-0.7855, 0.0), 0.0)


class TestItIsAlwaysAnswerable(unittest.TestCase):
    """0.0 rather than None, so a consumer never branches for a case that
    carries no information -- the same choice #1098 made for
    `participant_away_fraction`."""

    def test_no_delta_is_zero_not_none(self):
        self.assertEqual(_share(3.6485, 0.0), 0.0)

    def test_a_missing_delta_is_survivable(self):
        self.assertEqual(_share(3.6485, None), 0.0)

    def test_zero_regret_is_zero_not_a_division_error(self):
        """A share of nothing is not a finding."""
        self.assertEqual(_share(0.0, 2.6259), 0.0)
        self.assertEqual(_share(1e-12, 2.6259), 0.0)


class TestSignsAndScale(unittest.TestCase):
    def test_a_negative_regret_does_not_flip_the_meaning(self):
        """Regret can be negative -- that is #956's `regret_reliable`
        False case. A signed share would invert for a reason that has
        nothing to do with the pricing paths."""
        self.assertAlmostEqual(_share(-2.0, 1.0), 0.5, places=4)
        self.assertAlmostEqual(_share(2.0, -1.0), 0.5, places=4)

    def test_a_delta_larger_than_the_regret_clamps(self):
        """A delta exceeding the regret says the same thing as one equal
        to it: essentially all of the headline is the comparison. The
        field is a share, not a ratio to be read past its own scale."""
        self.assertEqual(_share(1.0, 5.0), 1.0)
        self.assertEqual(_share(1.0, 1.0), 1.0)

    def test_it_is_scale_free(self):
        """One threshold has to read the same on a $3 day and a $30 one."""
        self.assertAlmostEqual(_share(3.0, 1.5), _share(30.0, 15.0), places=6)


class TestItReachesThePublishedReport(unittest.TestCase):
    """A helper nothing calls is the shape this issue's own siblings keep
    taking -- source-checked the way #1109/#1111's equivalents are, since
    driving it needs a full real scored day."""

    def test_the_report_publishes_the_field(self):
        import ast
        from pathlib import Path

        src = Path(solver_writer.__file__.replace(".pyc", ".py")).read_text(
            encoding="utf-8"
        )
        self.assertIn('"regret_path_delta_share"', src)
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_compute_report_for_window"
        )
        called = {
            (
                n.func.attr
                if isinstance(n.func, ast.Attribute)
                else getattr(n.func, "id", "")
            )
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
        }
        self.assertIn(
            "_regret_path_delta_share",
            called,
            "the share is computed but never published, so a household "
            "still cannot tell a pricing disagreement from a dispatch one",
        )

    def test_it_is_not_in_the_per_row_history_tuple(self):
        """Belongs on top of the sensor, not in every history row -- the
        rows are size-constrained for the reason the recorder-cap guard
        exists."""
        self.assertNotIn(
            "regret_path_delta_share", solver_writer._QUALITY_HISTORY_FIELDS
        )


if __name__ == "__main__":
    unittest.main()
