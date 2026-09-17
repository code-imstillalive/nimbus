"""IV&V finding (since-4812f93 pass, 2026-09-18): #1089's own guard
(`solver/epr.py`, `compute_epr()` and `denominator_reason()`) claims a
single shared constant is "what keeps the two from disagreeing about
the same day" -- but the two functions test that constant with
MISMATCHED comparison operators, so they genuinely disagree at exactly
one boundary value.

`compute_epr()` treats a denominator as degenerate (no real opportunity
existed, report `epr = 1.0`) when:

    abs(theoretical_maximum_yield) < _DEGENERATE_YIELD_ABS      # strict <

`denominator_reason()` flags a NEGATIVE, non-degenerate denominator
(the #1089 failure mode -- the oracle priced worse than idle) when:

    j_star - j_ref > _DEGENERATE_YIELD_ABS                      # strict >

`theoretical_maximum_yield` is `j_ref - j_star`, so `j_star - j_ref ==
-theoretical_maximum_yield`. At `theoretical_maximum_yield == exactly
-_DEGENERATE_YIELD_ABS` (i.e. `j_star - j_ref == exactly
+_DEGENERATE_YIELD_ABS`):

    compute_epr():        abs(-1e-9) < 1e-9   -> False -> NOT degenerate
                           -> a real (wild) ratio is computed and
                              published, dividing by a ~1e-9 denominator
    denominator_reason(): 1e-9 > 1e-9         -> False -> NOT flagged

Both functions agree the value is "not degenerate" in the sense of
being outside compute_epr()'s own band, yet denominator_reason() -- the
guard built specifically to catch a negative, non-degenerate
denominator producing a wild, confidently-wrong-looking EPR -- fails to
flag the one case that is, by construction, exactly that: a negative
denominator, ~1e-9 in magnitude, treated by compute_epr() as real
enough to divide by.

This is narrow in practice -- it needs a real dollar computation to
land on the exact float `1e-9`, which is astronomically unlikely from
real j_ref/j_star arithmetic. It is nonetheless a real, reproducible
gap in a guard whose own docstring explicitly claims this exact class
of disagreement cannot happen ("Sharing the constant is what keeps the
two from disagreeing about the same day").

The pre-existing property test in tests/test_epr_denominator_reason.py
(checking the two never disagree across the band) does not catch this
either, because its OWN definition of "degenerate" independently
excludes the same exact boundary point -- so the property test and the
bug share the same blind spot.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from solver.epr import _DEGENERATE_YIELD_ABS, compute_epr, denominator_reason


class TestTheSharedBoundaryIsConsistent(unittest.TestCase):
    # FIXED 2026-09-18. `denominator_reason()` now compares `>=` rather
    # than `>`, so the boundary value this file pins is flagged and the
    # `xfail(strict=True)` marker that stood here has been removed --
    # with the fix in place it XPASSes, which is the point of the strict
    # marker.
    #
    # The operator was derived rather than picked: `compute_epr()` treats
    # a value as degenerate iff `abs(x) < EPS`, so it divides iff
    # `abs(x) >= EPS`, and "divides AND negative" is `x <= -EPS`, i.e.
    # `j_star - j_ref >= EPS`. Changing `compute_epr()` to `<=` would
    # close the same gap but by altering the older function's
    # long-standing behaviour at the boundary, so the change went here.
    def test_a_denominator_exactly_at_the_band_edge_is_flagged_if_not_degenerate(
        self,
    ):
        # j_star - j_ref == exactly _DEGENERATE_YIELD_ABS, so
        # theoretical_maximum_yield == j_ref - j_star == exactly
        # -_DEGENERATE_YIELD_ABS -- the precise boundary point.
        j_ref = 0.0
        j_star = _DEGENERATE_YIELD_ABS
        j_ach = -1.0

        result = compute_epr(j_ref=j_ref, j_ach=j_ach, j_star=j_star)
        reason = denominator_reason(j_ref=j_ref, j_star=j_star)

        is_treated_as_degenerate = result.epr == 1.0
        if not is_treated_as_degenerate:
            # compute_epr() decided this denominator was real enough to
            # divide by -- and it is negative (j_star > j_ref) -- so
            # denominator_reason() must agree it is a real, flaggable,
            # negative denominator. It must not silently pass it through
            # as if nothing were wrong.
            self.assertIsNotNone(
                reason,
                "compute_epr() treated theoretical_maximum_yield "
                f"({result.theoretical_maximum_yield!r}) as non-degenerate "
                f"and published epr={result.epr!r} from it, but "
                "denominator_reason() returned None for the identical "
                "inputs -- the two checks disagree at the shared "
                "boundary they claim cannot disagree.",
            )


if __name__ == "__main__":
    unittest.main()
