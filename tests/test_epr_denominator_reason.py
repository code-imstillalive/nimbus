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

**What causes it -- and what does NOT.** A first version of this file
called the condition structurally impossible, on the reasoning that the
oracle may always choose to do nothing. **That reasoning is wrong**, and
this repo had already measured why on 2026-09-16: `fixed_export_kw` pins
the oracle's export in every committed P2P period, so when the committed
export price is a fraction of the import price the commitment is *a loss
the oracle cannot decline* and idle is not in its feasible set at all.
#1001's own minimal reproduction -- a 12 kW commitment over seven hours
at 7.5c export against 37c import -- puts `j_star` **$2.43 worse than
doing nothing**. See `quality_report.py`'s
`_widen_export_pin_to_achieved()`, which documents it in full.

So there are two mechanisms, and the 17 Sep report carries evidence of
BOTH against a -1.4411 denominator:

    p2p_commitment_shortfall_kwh  2.0036   -> #1001, a committed pin was active
    j_star_path_delta             2.4889   -> #1081, the paths price differently

Either alone is large enough to account for the sign, and that report
cannot apportion between them. The reason string therefore names the
OBSERVATION (`oracle_not_better_than_idle`) and asserts no cause.

**Why flag it either way.** Under #1001 the arithmetic is sound and the
interpretation breaks: EPR measures capture against an idle baseline the
oracle was never free to choose, so it is not a fraction of anything
achievable. Under #1081 the two inputs are non-comparable. Both times
the published percentage is not a score, and both times the remedy is a
decision rather than a patch -- so this reports and leaves the number
alone.
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


class TestTheLegitimateCommitmentCase(unittest.TestCase):
    """#1001's own measured scenario, which is a REAL state rather than
    a computation fault, and must still be flagged.

    A 12 kW commitment over seven hours at 7.5c export against 37c
    import puts `j_star` $2.43 worse than doing nothing, because
    `fixed_export_kw` pins the export and the oracle cannot decline the
    loss. The arithmetic there is sound; what breaks is reading EPR as a
    capture fraction against a baseline the oracle was never free to
    choose.

    This is the case that makes the reason string's WORDING matter: it
    reports what was observed, not a diagnosis, because a household
    hitting this has nothing to fix in the code.
    """

    def test_a_committed_loss_still_gets_flagged(self):
        """j_star $2.43 worse than idle, #1001's figure."""
        self.assertEqual(
            denominator_reason(j_ref=0.0, j_star=2.43),
            "oracle_not_better_than_idle",
        )

    def test_the_reason_does_not_name_a_cause(self):
        """Two mechanisms can produce this and one of them is not a
        defect, so a reason that blamed either would be wrong half the
        time. If someone renames it to something diagnostic, this fails.
        """
        reason = denominator_reason(j_ref=REAL_J_REF, j_star=REAL_J_STAR)
        self.assertNotIn("1081", reason)
        self.assertNotIn("1001", reason)
        for blamed in ("mismatch", "path", "commitment", "bug", "invalid"):
            self.assertNotIn(
                blamed,
                reason,
                f"the reason string names a cause ({blamed!r}); both #1001 "
                "and #1081 can produce this condition and #1001 is a real "
                "state rather than a defect, so naming either is wrong "
                "roughly half the time",
            )


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
    """`compute_epr()`'s pre-existing "no opportunity existed" branch
    reports 1.0, and the reason must stay silent across the whole of
    that band -- otherwise a genuinely flat day is labelled both perfect
    AND unreliable, off a denominator that is float noise."""

    def test_exact_zero_denominator_reports_one_and_no_reason(self):
        r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0)
        self.assertEqual(r.epr, 1.0)
        self.assertIsNone(r.denominator_reason)

    def test_float_noise_inside_the_degenerate_band_is_not_flagged(self):
        """j_star above j_ref by a hair. `epr` is already 1.0 here, so a
        reason would contradict it. This is the case a bare `j_star >
        j_ref` test got wrong."""
        r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0 + 1e-12)
        self.assertEqual(r.epr, 1.0)
        self.assertIsNone(
            r.denominator_reason,
            "a -1e-12 denominator on a flat day is float noise, and "
            "flagging it makes epr_reliable False for a day that is "
            "simply uneventful -- while epr itself still reads 1.0",
        )

    def test_the_two_checks_never_disagree_across_the_band(self):
        """The property that matters, asserted in BOTH directions.

        **This test was one-sided until #1104, and that is how Mark
        Purcell's IV&V pass found a real gap in code this file was
        supposed to be guarding.** It asserted only that a day is never
        *both* degenerate and flagged. The dangerous direction is the
        other one -- a denominator `compute_epr()` considers real enough
        to divide by, negative, and NOT flagged -- and nothing here
        looked for it.

        There were two independent reasons it slipped through, and the
        second is the more instructive:

        1. the assertion covered one direction of a biconditional;
        2. the fixture could not reach the boundary anyway. `3.0 +/-
           delta` never lands on exactly `-1e-9` after float rounding, so
           even a two-sided assertion would have passed. `j_ref=0.0`
           below makes the subtraction exact, which is what it takes.
        """
        for delta in (0.0, 1e-12, 1e-10, 5e-10, 1e-9, 1e-8, 1e-6, 1.4411):
            for sign in (1.0, -1.0):
                with self.subTest(delta=delta, sign=sign):
                    r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0 + sign * delta)
                    degenerate = (
                        r.epr == 1.0 and abs(r.theoretical_maximum_yield) < 1e-9
                    )
                    self.assertFalse(
                        degenerate and r.denominator_reason is not None,
                        "the same day was reported as a perfect 1.0 and as "
                        "having an unusable denominator",
                    )

    def test_a_divided_by_negative_denominator_is_always_flagged(self):
        """The direction the test above was missing, on a fixture that
        can actually reach the boundary: `j_ref=0.0` makes `j_ref -
        j_star` exact, so the `== -EPS` case is genuinely tested rather
        than approached."""
        from solver import epr as epr_mod

        eps = epr_mod._DEGENERATE_YIELD_ABS
        for j_star in (eps, 2 * eps, 10 * eps, 1e-6, 1.4411):
            with self.subTest(j_star=j_star):
                r = compute_epr(j_ref=0.0, j_ach=4.0, j_star=j_star)
                if abs(r.theoretical_maximum_yield) < eps:
                    continue  # degenerate: compute_epr() reports 1.0, correctly
                self.assertIsNotNone(
                    r.denominator_reason,
                    f"j_star={j_star!r} gives a NEGATIVE denominator "
                    f"({r.theoretical_maximum_yield!r}) that compute_epr() "
                    f"divided by -- publishing epr={r.epr!r} -- with no "
                    "reason attached. This is #1104: two strict operators "
                    "around one shared boundary leave exactly this gap",
                )

    def test_the_real_day_is_far_outside_the_band(self):
        """Guards against anyone widening _DEGENERATE_YIELD_ABS far
        enough to swallow the defect this check exists for."""
        from solver import epr as epr_mod

        self.assertLess(
            epr_mod._DEGENERATE_YIELD_ABS,
            abs(REAL_J_REF - REAL_J_STAR) / 1e3,
            "the degenerate band has grown close enough to the real "
            "-1.4411 denominator to risk swallowing it",
        )


if __name__ == "__main__":
    unittest.main()
