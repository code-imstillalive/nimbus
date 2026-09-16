"""nimbus issue #773: the pinned re-solve runs under the tolerance its
own solution was accepted under.

`phase2_pin_resolve` has been coming back `Infeasible` on a real install,
which contradicts the phase's own stated invariant -- it pins every
binary to the value the previous solve just returned, so that solution is
feasible by construction and the re-solve should reproduce it.

Both candidates the code comment listed are eliminated:

* **Unconditional rounding snapping a non-integral value.** The
  integrality diagnostic added for exactly this shipped in v0.94.336 and
  has never fired on a failing cycle across four failures, so the worst
  gap is under 1e-6.
* **`h.val()` returning a node relaxation.** It is a thin read of
  `getSolution().col_value`, which after a `kOptimal` MIP solve is the
  incumbent.

What is left is HiGHS's own defaults, and they do not agree across the
MIP/LP boundary:

    mip_feasibility_tolerance      1e-06
    primal_feasibility_tolerance   1e-07

A branch-and-bound incumbent is accepted satisfying the rows to 1e-6; the
pinned re-solve is a pure LP and demands 1e-7. An incumbent anywhere in
that band is feasible for the MIP that produced it and infeasible for the
LP asked to re-certify it. That also explains the intermittency: it needs
the incumbent to land in the band rather than below it.

Pinning changes only COLUMN bounds, so no row bound moves and the
incumbent's row activities are untouched -- which is why tolerance, not
structure, is the remaining explanation.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import highspy
from solver import lp


class TestTheToleranceMismatchIsReal(unittest.TestCase):
    """The premise, checked against the installed solver rather than
    asserted from documentation. If HiGHS ever changes these defaults so
    they agree, this fix becomes unnecessary and this test says so."""

    def test_mip_tolerance_is_looser_than_lp_tolerance(self):
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        _s, lp_tol = h.getOptionValue("primal_feasibility_tolerance")
        _s, mip_tol = h.getOptionValue("mip_feasibility_tolerance")
        self.assertGreater(
            mip_tol,
            lp_tol,
            "the whole #773 pin-resolve explanation rests on a MIP incumbent "
            "being accepted under a LOOSER tolerance than the LP that then "
            "re-certifies it -- if these now agree, revisit the fix",
        )


class TestTheContextManagerWidensAndRestores(unittest.TestCase):
    def setUp(self):
        self.h = highspy.Highs()
        self.h.setOptionValue("output_flag", False)

    def _lp_tol(self):
        _s, v = self.h.getOptionValue("primal_feasibility_tolerance")
        return v

    def _mip_tol(self):
        _s, v = self.h.getOptionValue("mip_feasibility_tolerance")
        return v

    def test_inside_the_block_the_lp_tolerance_matches_the_mip_one(self):
        before = self._lp_tol()
        with lp._lp_tolerance_matching_mip(self.h):
            self.assertEqual(self._lp_tol(), self._mip_tol())
        self.assertEqual(self._lp_tol(), before)

    def test_the_previous_value_is_restored_after_an_exception(self):
        """A widened tolerance leaking into the phases after this one
        would silently loosen a genuine LP solve -- worse than the bug
        being fixed, and invisible."""
        before = self._lp_tol()
        with (
            self.assertRaises(RuntimeError),
            lp._lp_tolerance_matching_mip(self.h),
        ):
            raise RuntimeError("phase blew up")
        self.assertEqual(self._lp_tol(), before)

    def test_an_already_looser_lp_tolerance_is_left_alone(self):
        """The fix widens, never narrows. If an install has deliberately
        set a looser LP tolerance than the MIP's, tightening it here
        would be an unrequested behaviour change."""
        loose = self._mip_tol() * 100
        self.h.setOptionValue("primal_feasibility_tolerance", loose)
        with lp._lp_tolerance_matching_mip(self.h):
            self.assertEqual(self._lp_tol(), loose)
        self.assertEqual(self._lp_tol(), loose)


class TestItNeverBreaksTheSolve(unittest.TestCase):
    def test_a_solver_that_refuses_option_reads_is_survived(self):
        """Every read here is best-effort: a diagnostic-shaped widening
        must never be the reason a real solve cycle dies."""

        class _Hostile:
            def getOptionValue(self, _name):
                raise RuntimeError("no options for you")

            def setOptionValue(self, _name, _value):
                raise RuntimeError("no options for you")

        ran = []
        with lp._lp_tolerance_matching_mip(_Hostile()):
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_a_solver_that_fails_only_on_restore_is_survived(self):
        class _FailsOnSet:
            def __init__(self):
                self.calls = 0

            def getOptionValue(self, name):
                return (0, 1e-7 if "primal" in name else 1e-6)

            def setOptionValue(self, _name, _value):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("restore failed")

        h = _FailsOnSet()
        with lp._lp_tolerance_matching_mip(h):
            pass
        self.assertEqual(h.calls, 2)


if __name__ == "__main__":
    unittest.main()
