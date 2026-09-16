"""nimbus issue #773: the lex phase-2 tie bound carries a numerical slack.

Observed live: `phase2_secondary` returned `Infeasible` on a cycle where
`phase1_primary` had just returned `Optimal` in 0.9 s / 1,906 iterations.

That is a contradiction, not a difficulty. Phase 2's feasible set is
phase 1's intersected with `primary <= p*`, and phase 1's own optimum
satisfies that by construction — so phase 2 cannot be empty. Unless the
bound excludes that optimum, which is what using it verbatim does:
`p*` is the objective value HiGHS *reports*, while the row activity
HiGHS then recomputes sums ~12k float terms independently. The two agree
only to rounding, and a recomputed activity landing above `p*` by more
than the feasibility tolerance cuts off the very point the bound was
derived from.

**The architecture's guarantee is untouched**, and this file pins that
rather than leaving it to the prose. `_solve_with_options()` commits to
secondary never overriding a real price signal *"not even by an
epsilon"*. The slack is the solver's own primal feasibility tolerance
made explicit — ~1e-7 dollars, a ten-millionth of a cent. It cannot
express a price. The tests below assert it stays that small, so a future
edit that turns it into an economic epsilon fails here.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import highspy
from solver import lp


def _slack(primary_value: float) -> float:
    """The same expression the phase-2 bound uses."""
    return max(
        lp._LEX_PRIMARY_TIE_ABS_SLACK,
        abs(primary_value) * lp._LEX_PRIMARY_TIE_REL_SLACK,
    )


class TestTheSlackIsNumericalNotEconomic(unittest.TestCase):
    """If any of these start failing, the slack has stopped being a
    rounding allowance and become a price concession."""

    def test_it_is_far_below_one_cent_at_realistic_objective_values(self):
        # A day's primary objective on a real install runs to tens of
        # dollars; a large fleet could reach hundreds.
        for primary_value in (0.0, 1.0, 20.0, 100.0, 1_000.0):
            with self.subTest(primary_value=primary_value):
                self.assertLess(
                    _slack(primary_value),
                    1e-5,
                    "a slack approaching a thousandth of a cent is no longer "
                    "'the solver's own tolerance' -- it is an economic "
                    "epsilon, which this architecture explicitly refuses",
                )

    def test_it_is_at_least_the_solver_primal_tolerance(self):
        """Smaller than HiGHS's own feasibility tolerance would make the
        slack decorative: the row activity is only checked to 1e-7, so a
        slack below that cannot absorb the error it exists for."""
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        _s, primal_tol = h.getOptionValue("primal_feasibility_tolerance")
        self.assertGreaterEqual(_slack(0.0), float(primal_tol))

    def test_it_is_orders_of_magnitude_tighter_than_the_secondary_epsilon(self):
        """LexOptions phase 3 already applies `max(1e-6, |secondary| *
        1e-6)` on the SECONDARY side. This one is on the primary side --
        the side the guarantee protects -- so it must be much tighter,
        not merely different."""
        secondary_epsilon = max(1e-6, abs(20.0) * 1e-6)
        self.assertLess(_slack(20.0), secondary_epsilon / 10)

    def test_the_relative_term_only_matters_for_large_objectives(self):
        """The absolute term should dominate at ordinary magnitudes, so
        a household-scale solve gets exactly the solver tolerance and
        nothing more."""
        self.assertEqual(_slack(20.0), lp._LEX_PRIMARY_TIE_ABS_SLACK)
        self.assertGreater(_slack(1e6), lp._LEX_PRIMARY_TIE_ABS_SLACK)


class TestTheBoundActuallyAdmitsItsOwnOptimum(unittest.TestCase):
    """The behavioural point: a point whose recomputed activity sits a
    hair above the reported optimum must stay feasible.

    Built directly rather than through the solver, because reproducing
    float drift across a 12k-term sum on demand is not something a test
    can do reliably -- what it *can* do is assert the bound is wide
    enough to absorb drift of the size the solver tolerates.
    """

    def test_a_point_over_by_the_solver_tolerance_is_still_admitted(self):
        primary_value = 20.0
        bound = primary_value + _slack(primary_value)
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        _s, primal_tol = h.getOptionValue("primal_feasibility_tolerance")
        drifted_activity = primary_value + float(primal_tol)
        self.assertLessEqual(
            drifted_activity,
            bound,
            "a recomputed activity that exceeds the reported optimum by the "
            "solver's own tolerance is exactly the case that made "
            "phase2_secondary infeasible -- the bound must admit it",
        )

    def test_the_unslacked_bound_would_have_rejected_it(self):
        """Pins the defect itself: without the slack the same point is
        outside the bound, which is how a phase could be infeasible
        against its own optimum."""
        primary_value = 20.0
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        _s, primal_tol = h.getOptionValue("primal_feasibility_tolerance")
        drifted_activity = primary_value + float(primal_tol)
        self.assertGreater(drifted_activity, primary_value)


if __name__ == "__main__":
    unittest.main()
