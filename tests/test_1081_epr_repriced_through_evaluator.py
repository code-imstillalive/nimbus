"""nimbus #1081: EPR and regret are computed from `j_star_evaluator`,
not the raw LP objective.

**Decided by Mark Purcell, 2026-09-18**, after this issue's diagnostic
groundwork measured the two paths apart. His reasoning, which is the part
measurement could not supply:

> The soft-SoC penalty, slack penalties, and the bonus-as-a-chosen-
> variable all exist to shape what the LP searches for, not to describe a
> cost the household could ever actually pay. They're search machinery.

There is no day on which a household paid a soft-SoC penalty, so pricing
an already-fixed trajectory against one produces an arbitrary number.
`j_star_evaluator` reprices the **same chosen trajectory** through the
**same real-cost arithmetic** `j_ach` already uses.

**The LP did not change.** `j_star` is still computed, still published,
and is still the right thing to optimise -- it is what CHOOSES the plan.
Only which of two already-published numbers the headline derives from has
changed. That distinction is Mark's explicit requirement and
`TestJStarIsStillPublished` below is what keeps it true.

## Why this is a consistency fix, not a choice between two arbitrary options

`hourly_regret` has **always** been evaluator-priced. `quality_report.py`
computes the oracle's per-period cost through `evaluate_realized_cost_
multi()` specifically so the hourly shapes are comparable, and
`j_star_evaluator` is derived from that very object:

    oracle_residual   = evaluate_realized_cost_multi(...)   <- hourly_regret
    j_star_evaluator  = oracle_residual.total_cost - bonus  <- now the headline

Only the headline reached for the LP objective. So this **narrows** the
documented sum-of-hours-vs-headline gap rather than widening it: what
remains is the bonus-credit term alone, not the whole path mismatch. An
earlier draft of the CHANGELOG entry for this change claimed the
opposite, and would have shipped a caveat that was exactly backwards.

## What it does to a real day

2026-09-17 on the reference install, every figure read off the published
report:

```
                    before (LP obj)      after (evaluator)
j_star used              6.9308               4.4419
denominator             -1.4411              +1.0478
regret_dollars          +2.0564              +4.5453
EPR                    +242.70%             -333.79%
```

The published EPR and the honest one were **opposite in sign**. #1089
established that the +242.7% was a sign-cancellation artefact on a day the
household spent $3.50 more than leaving the battery idle, so a strongly
negative EPR is the correct reading of that day, not a regression.

**This rescores every historical day on every install**, which is why it
is its own change rather than folded into #1089's guard, and why any
"EPR improved from X to Y" comparison spanning it is invalid.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.epr import compute_epr
from test_jstar_path_delta import N, _report

# The real 2026-09-17 figures, for the arithmetic pin below.
REAL_J_REF = 5.4897
REAL_J_ACH = 8.9872
REAL_J_STAR = 6.9308
REAL_J_STAR_EVALUATOR = 4.4419


# A bonus-priced oracle is what makes the two paths actually differ --
# the LP CHOOSES which periods claim the capped bonus volume while the
# evaluator ALLOCATES it after the fact. Same fixture shape
# test_jstar_path_delta.py uses to measure that split.
def _bonus_price():
    bonus = np.zeros(N)
    bonus[17:24] = 0.20
    return bonus


BONUS_KWARGS = {"bonus_volume": 20.0, "real_p2p": 3.0}


class TestEprUsesTheEvaluatorPath(unittest.TestCase):
    """Behavioural, through a real `compute_quality_report()` -- not a
    source check. The whole point is which number comes out."""

    def setUp(self):
        self.report = _report(bonus_price=_bonus_price(), **BONUS_KWARGS)

    def test_the_two_paths_actually_differ_in_this_fixture(self):
        """Guard on the guard. If the fixture stopped producing a path
        delta, every assertion below would pass whichever number was
        used, and this file would be worthless without saying so."""
        self.assertNotAlmostEqual(
            self.report.j_star,
            self.report.j_star_evaluator,
            places=6,
            msg="this fixture no longer separates the two pricing paths, "
            "so the assertions below cannot tell them apart -- restore a "
            "bonus-priced oracle or find another source of delta",
        )

    def test_epr_matches_the_evaluator_repricing(self):
        expected = compute_epr(
            j_ref=self.report.j_ref,
            j_ach=self.report.j_ach,
            j_star=self.report.j_star_evaluator,
        )
        self.assertAlmostEqual(self.report.epr.epr, expected.epr, places=9)

    def test_epr_does_not_match_the_raw_lp_objective(self):
        """The assertion that would have failed before this change."""
        lp_priced = compute_epr(
            j_ref=self.report.j_ref,
            j_ach=self.report.j_ach,
            j_star=self.report.j_star,
        )
        self.assertNotAlmostEqual(self.report.epr.epr, lp_priced.epr, places=9)

    def test_theoretical_maximum_yield_is_the_repriced_denominator(self):
        self.assertAlmostEqual(
            self.report.epr.theoretical_maximum_yield,
            self.report.j_ref - self.report.j_star_evaluator,
            places=9,
        )

    def test_uplift_available_is_repriced_too(self):
        """`uplift_available` is the same quantity `regret_dollars` is,
        computed inside EPRResult. If it kept the LP objective while the
        ratio moved, the report would carry two regrets that disagree."""
        self.assertAlmostEqual(
            self.report.epr.uplift_available,
            self.report.j_ach - self.report.j_star_evaluator,
            places=9,
        )


class TestJStarIsStillPublished(unittest.TestCase):
    """Mark's explicit requirement, and the thing most likely to be
    'tidied' away later by someone who reads the LP objective as now
    unused:

    > `j_star` the **LP objective** doesn't disappear -- it's still the
    > correct thing to optimize, and still worth publishing separately
    > for anyone diagnosing the solver itself.
    """

    def setUp(self):
        self.report = _report(bonus_price=_bonus_price(), **BONUS_KWARGS)

    def test_the_lp_objective_is_still_on_the_report(self):
        self.assertIsInstance(self.report.j_star, float)

    def test_the_evaluator_repricing_is_still_on_the_report(self):
        self.assertIsInstance(self.report.j_star_evaluator, float)

    def test_the_gap_between_them_is_still_published(self):
        """`j_star_path_delta` is what made this decision possible. It
        stays, because it is now the only way to see that the two paths
        disagree at all -- the headline no longer exposes it.

        Compared at 3 places, not 9, and the reason is a real quirk
        rather than slack: the report publishes `j_star_evaluator` and
        `j_star_path_delta` ROUNDED to 4dp while `j_star` is carried
        unrounded, so differencing the two published fields cannot
        reproduce the published delta exactly. Measured here: 0.4559521
        against a published 0.456. Asserting 9 places would be pinning
        the rounding, not the identity.
        """
        self.assertAlmostEqual(
            self.report.j_star_path_delta,
            self.report.j_star - self.report.j_star_evaluator,
            places=3,
        )


class TestTheRealDayArithmetic(unittest.TestCase):
    """Pins the published before/after on 2026-09-17 so the CHANGELOG's
    numbers can be checked rather than taken on trust."""

    def test_before_and_after_are_opposite_in_sign(self):
        before = compute_epr(j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR)
        after = compute_epr(
            j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR_EVALUATOR
        )
        self.assertAlmostEqual(before.epr, 2.4270, places=3)
        self.assertAlmostEqual(after.epr, -3.3379, places=3)
        self.assertGreater(before.epr, 0.0)
        self.assertLess(after.epr, 0.0)

    def test_the_denominator_stops_being_negative(self):
        after = compute_epr(
            j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR_EVALUATOR
        )
        self.assertGreater(after.theoretical_maximum_yield, 0.0)
        self.assertIsNone(
            after.denominator_reason,
            "#1089's guard should stop firing on this day once the two "
            "sides are priced alike -- if it still fires, the repricing "
            "is not the whole story for this day",
        )

    def test_regret_stays_non_negative_on_this_day(self):
        """Repricing moves regret from +2.0564 to +4.5453. Both are
        non-negative, so this day does not newly trip #956's guard --
        worth pinning, because a change that rescores history could
        plausibly have pushed a clean day into an unreliable one."""
        self.assertGreaterEqual(REAL_J_ACH - REAL_J_STAR_EVALUATOR, 0.0)


class TestBothWritersAgree(unittest.TestCase):
    """The standalone/cron writer computes `regret_dollars` itself rather
    than reading it off the report, so it is a second place that must
    reprice or the two deployments silently disagree (#357)."""

    def test_neither_writer_still_subtracts_the_raw_lp_objective(self):
        import os

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for relative in (
            os.path.join("custom_components", "nimbus_load", "solver_writer.py"),
            os.path.join(
                "docs",
                "real-world-integration",
                "files",
                "nimbus_solver_quality_writer.py",
            ),
        ):
            with self.subTest(writer=relative):
                with open(os.path.join(here, relative), encoding="utf-8") as f:
                    text = " ".join(f.read().split())
                self.assertNotIn(
                    "regret_dollars = report.j_ach - report.j_star )",
                    text.replace("( ", "("),
                )
                self.assertIn("report.j_ach - report.j_star_evaluator", text)


if __name__ == "__main__":
    unittest.main()
