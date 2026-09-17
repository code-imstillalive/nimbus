"""nimbus #1089: EPR's denominator has never been checked, and an
unchecked denominator can make a bad day publish as a high score.

    EPR = (j_ref - j_ach) / (j_ref - j_star)

A negative NUMERATOR is honest: the household did worse than the idle
baseline, EPR goes negative, the dashboard says so. A negative
DENOMINATOR inverts that -- two negatives divide to a positive.

**Measured on a real install, 2026-09-17.**

    j_ref   5.4897      numerator    j_ref - j_ach = -3.4975
    j_ach   8.9872      denominator  j_ref - j_star = -1.4411
    j_star  6.9308      EPR = +2.427   ->   published as 242.7%

The household spent **$3.50 more than leaving the battery alone** and
the report published **242.7%**.

**Why nothing caught it.** Three reliability signals now exist; only one
of them existed in a form that could see this, and it did not:

    soc_discrepancy_reliable   tests the SoC reconstruction   (no j_ref)
    regret_reliable            tests j_ach - j_star           (no j_ref)
    epr_denominator_reason     tests j_ref - j_star           <- new

`regret_dollars` that day was **+2.0564** -- positive, so
`regret_reliable` was True and `epr_reason` was None. The day was
flagged at all only by an unrelated SoC disagreement. With a clean
reconstruction, 242.7% would have published as fully reliable.

**Why the condition is always a computation fault.** `j_star <= j_ref`
holds by construction when both are priced by the same model: the oracle
may always choose to do nothing, so the idle baseline is inside its
feasible set. A violation is never a property of the day. On this
install the cause is #1081 -- `j_star` is the LP objective (carrying
soft-SoC and slack penalties) while `j_ref` comes from the independent
evaluator. Re-pricing the same oracle plan through the evaluator gave
4.4419 against the LP's 6.9308, restoring a **+1.0478** denominator and
an honest EPR of **-3.34**.

So the fix reports rather than repairs: which side should move rescores
every historical day, and that is #1081's decision.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from solver.epr import compute_epr, denominator_reason

# The three real published figures, 2026-09-17, reference install
# v0.94.382. Kept as module constants because four separate tests assert
# different things about the same day and a typo in one of them would
# otherwise be invisible.
REAL_J_REF = 5.4897
REAL_J_ACH = 8.9872
REAL_J_STAR = 6.9308
# The same oracle plan re-priced through the evaluator instead of read
# off the LP objective -- #1081's own instrument, published as
# `j_star_evaluator` on the same report.
REAL_J_STAR_EVALUATOR = 4.4419


class TestTheSignTestItself(unittest.TestCase):
    def test_the_normal_case_is_silent(self):
        """The oracle beats idle, which is the overwhelmingly common
        case and must never be flagged."""
        self.assertIsNone(denominator_reason(j_ref=5.366, j_star=-22.0619))

    def test_the_real_day_is_flagged(self):
        self.assertEqual(
            denominator_reason(j_ref=REAL_J_REF, j_star=REAL_J_STAR),
            "oracle_not_better_than_idle",
        )

    def test_equality_is_not_flagged(self):
        """`j_star == j_ref` means no value was available -- a real day
        with genuinely flat prices and nothing to arbitrage. It is a
        degenerate denominator, not an impossible one, and compute_epr()
        already owns that case by reporting 1.0. Flagging it here would
        cry wolf on a legitimate day."""
        self.assertIsNone(denominator_reason(j_ref=3.0, j_star=3.0))

    def test_a_negative_numerator_alone_is_not_flagged(self):
        """The household doing worse than idle is a real outcome, not a
        fault. With a sound denominator it produces a correctly NEGATIVE
        EPR, which is honest and needs no reason code -- this check is
        about the denominator only."""
        self.assertIsNone(denominator_reason(j_ref=5.4897, j_star=4.4419))


class TestItRidesOnTheResult(unittest.TestCase):
    """Carried on EPRResult so every consumer gets it without opting
    in -- the native integration, the standalone cron writer, and
    anything built later."""

    def test_the_real_day_carries_the_reason(self):
        r = compute_epr(j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR)
        self.assertEqual(r.denominator_reason, "oracle_not_better_than_idle")

    def test_a_clean_day_carries_none(self):
        """16 Sep's real figures from the same three-day history -- the
        one day of the three with sound arithmetic."""
        r = compute_epr(j_ref=5.366, j_ach=-20.8856, j_star=-22.0619)
        self.assertIsNone(r.denominator_reason)
        self.assertAlmostEqual(r.epr, 0.9571, places=4)

    def test_the_published_number_is_reproduced_exactly(self):
        """Pins the arithmetic that produced 242.7%, so nobody has to
        take this file's word for the headline figure."""
        r = compute_epr(j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR)
        self.assertAlmostEqual(r.value_captured, -3.4975, places=4)
        self.assertAlmostEqual(r.theoretical_maximum_yield, -1.4411, places=4)
        self.assertAlmostEqual(r.epr, 2.427, places=3)

    def test_both_signs_negative_is_what_makes_it_look_good(self):
        """The actual mechanism, asserted rather than described: the
        published ratio is positive while BOTH of its inputs are
        negative. This is the property that makes an unchecked
        denominator worse than an unchecked numerator."""
        r = compute_epr(j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR)
        self.assertLess(r.value_captured, 0.0)
        self.assertLess(r.theoretical_maximum_yield, 0.0)
        self.assertGreater(r.epr, 1.0)

    def test_repricing_the_oracle_restores_an_honest_number(self):
        """#1081's own instrument applied to the same day: price j_star
        through the evaluator instead of the LP objective and the
        denominator turns positive, the reason clears, and the EPR
        becomes the strongly negative number the day earned."""
        r = compute_epr(
            j_ref=REAL_J_REF, j_ach=REAL_J_ACH, j_star=REAL_J_STAR_EVALUATOR
        )
        self.assertIsNone(r.denominator_reason)
        self.assertGreater(r.theoretical_maximum_yield, 0.0)
        self.assertAlmostEqual(r.theoretical_maximum_yield, 1.0478, places=4)
        self.assertLess(r.epr, -3.0)


class TestTheWarningHasItsOwnDedupSet(unittest.TestCase):
    """The publish site logs this once per scored day, and the dedup set
    it uses must be its OWN.

    This is not a stylistic point. On the real 2026-09-17 day the
    denominator condition fired while `regret_reliable` was True, so a
    shared set would have let whichever condition was seen first silence
    the other -- and for a day where only the denominator is wrong, that
    means no warning at all on the one condition whose failure mode
    makes the headline look better than the truth.

    Checked as object identity rather than by driving the publish
    function, which needs a live HA instance and a real recorder day.
    """

    def test_all_three_warned_sets_are_distinct_objects(self):
        import solver_writer

        sets = {
            "soc_discrepancy (#538)": id(
                solver_writer._QUALITY_REPORT_UNRELIABLE_WARNED
            ),
            "negative_regret (#956)": id(
                solver_writer._QUALITY_REPORT_NEGATIVE_REGRET_WARNED
            ),
            "epr_denominator (#1089)": id(
                solver_writer._QUALITY_REPORT_EPR_DENOMINATOR_WARNED
            ),
        }
        self.assertEqual(
            len(set(sets.values())),
            3,
            "two or more of the quality-report warning dedup sets are the "
            f"same object ({sets}) -- whichever condition is seen first "
            "would silence the others for that day",
        )

    def test_the_publish_site_gates_on_the_new_reason(self):
        """Source check, for the same reason
        `test_solver_writer_family_a_freshness_repush.py` uses one: the
        condition cannot be produced on demand, and the wiring is what
        regresses."""
        import solver_writer

        src = solver_writer.__file__.replace(".pyc", ".py")
        with open(src, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        self.assertIn(
            'day_entry.get("epr_denominator_reason") is not None',
            text,
            "the publish site no longer gates a WARNING on the denominator "
            "reason, so a sign-inverted EPR would publish silently",
        )
        self.assertIn(
            "_QUALITY_REPORT_EPR_DENOMINATOR_WARNED.add(yesterday_key)",
            text,
        )


class TestTheDegenerateCaseStillWorks(unittest.TestCase):
    def test_exact_zero_denominator_reports_one_and_no_reason(self):
        """compute_epr()'s pre-existing `abs(...) < 1e-9 -> 1.0` branch
        is untouched by this change, and must not start emitting a
        reason: no value available means nothing was missed."""
        r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0)
        self.assertEqual(r.epr, 1.0)
        self.assertIsNone(r.denominator_reason)


if __name__ == "__main__":
    unittest.main()
