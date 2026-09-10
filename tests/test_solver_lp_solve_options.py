"""Direct test coverage for nimbus issue #696, Stage 1: `LPProblem`'s new
primary/secondary objective architecture (`LexOptions` / `BlendedOptions` /
`CalibratedOptions`), ported from the sibling HAEO integration's own
`network.py` (fetched and read in full via `gh api` before writing this,
not reconstructed from a summary -- see `lp.py`'s own top-of-file comment).

Two hand-checkable synthetic scenarios, both `x1 + x2 == 10.0`, drive
every assertion below:

- `_genuine_tie_problem()`: primary cost is IDENTICAL for x1 and x2 (a
  real tie, multiple primary-optimal solutions exist), secondary prefers
  x2 as large as possible. Proves secondary CAN break a genuine tie.
- `_real_price_difference_problem()`: primary cost strongly prefers x1
  over x2 (a REAL, large economic difference -- x2 costs 100x as much),
  secondary prefers the OPPOSITE (x2 as large as possible). Proves
  secondary can NEVER override a real primary cost difference, even
  though it's actively pulling the other way -- the one guarantee this
  whole architecture exists for.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from solver.lp import BlendedOptions, CalibratedOptions, LexOptions, LPProblem


def _genuine_tie_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
    # Secondary: minimizing -x2 is the same as maximizing x2 -- a
    # preference for x2 as LARGE as possible among primary-optimal ties.
    p.set_secondary_cost("x2", -1.0)
    return p


def _real_price_difference_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=100.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
    # Secondary actively opposes the real primary preference -- wants x2
    # as large as possible, the exact opposite of what primary wants.
    p.set_secondary_cost("x2", -1.0)
    return p


class TestOptionsNoneIsByteIdenticalToPreExistingBehavior(unittest.TestCase):
    def test_explicit_none_matches_implicit_default(self):
        p1 = _genuine_tie_problem()
        p2 = _genuine_tie_problem()
        r1 = p1.solve()
        r2 = p2.solve(options=None)
        self.assertEqual(r1.status, r2.status, "optimal")
        self.assertAlmostEqual(r1.objective, r2.objective)
        self.assertAlmostEqual(p1.value_of(r1, "x2"), p2.value_of(r2, "x2"))

    def test_secondary_cost_is_never_read_without_options(self):
        """A problem with real secondary costs set but solved WITHOUT
        options= must land exactly where plain primary-only optimization
        would -- the genuine tie is NOT broken, since options=None never
        even looks at _secondary_cost."""
        p = _genuine_tie_problem()
        result = p.solve()
        self.assertEqual(result.status, "optimal")
        self.assertAlmostEqual(result.objective, 10.0)
        # A genuine LP tie with no tie-break active -- HiGHS picks
        # SOME feasible vertex, but nothing here should assert which
        # one; only that the OBJECTIVE (primary cost) is the real 10.0
        # regardless of the untouched secondary cost sitting on x2.


class TestLexOptionsBreaksAGenuineTie(unittest.TestCase):
    def test_secondary_drives_x2_to_its_lex_optimal_extreme(self):
        p = _genuine_tie_problem()
        result = p.solve(options=LexOptions())
        self.assertEqual(result.status, "optimal")
        # Primary cost must be EXACTLY the true optimum (10.0) -- lex's
        # whole guarantee is that phase 2/3 never worsen it, not even
        # by an epsilon.
        self.assertAlmostEqual(result.objective, 10.0, places=6)
        # Secondary (minimize -x2) pushes x2 to its own true maximum
        # (10.0) among primary-optimal solutions.
        self.assertAlmostEqual(p.value_of(result, "x2"), 10.0, places=4)
        self.assertAlmostEqual(p.value_of(result, "x1"), 0.0, places=4)


class TestLexOptionsNeverOverridesARealPriceDifference(unittest.TestCase):
    def test_secondary_cannot_move_x2_off_its_true_optimum(self):
        p = _real_price_difference_problem()
        result = p.solve(options=LexOptions())
        self.assertEqual(result.status, "optimal")
        # x2 costs 100x as much -- the true optimum is x1=10, x2=0,
        # primary=10.0, regardless of secondary actively wanting x2
        # large.
        self.assertAlmostEqual(p.value_of(result, "x1"), 10.0, places=4)
        self.assertAlmostEqual(p.value_of(result, "x2"), 0.0, places=4)
        self.assertAlmostEqual(result.objective, 10.0, places=6)


class TestBlendedOptionsMatchesHandComputedArithmetic(unittest.TestCase):
    def test_explicit_small_weight_breaks_the_tie_the_same_way_as_lex(self):
        p = _genuine_tie_problem()
        result = p.solve(options=BlendedOptions(blend_weight=0.01))
        self.assertEqual(result.status, "optimal")
        # Blended objective is x1 + x2 - 0.01*x2 = x1 + 0.99*x2, subject
        # to x1+x2=10 -- minimized by pushing x2 as HIGH as possible
        # (its blended coefficient 0.99 < x1's 1.0), same direction
        # LexOptions found, by hand-computed arithmetic not just
        # "didn't crash".
        self.assertAlmostEqual(p.value_of(result, "x2"), 10.0, places=4)
        self.assertAlmostEqual(p.value_of(result, "x1"), 0.0, places=4)

    def test_a_weight_large_enough_can_override_a_real_price_difference(self):
        """Honest proof BlendedOptions has NO structural safety net --
        unlike Lex/Calibrated, a hand-picked weight large enough WILL
        distort the real primary answer. This is exactly the failure
        mode #696 exists to move away from (this project's own pre-
        #696 tie-break mechanisms had to verify their own magnitude
        stayed safe by hand, forever, as more get added)."""
        p = _real_price_difference_problem()
        # blend_weight=10 makes x2's blended coefficient 100 - 10 = 90,
        # still more than x1's 1.0 -- not enough to flip it. Push much
        # higher to actually demonstrate the override.
        result = p.solve(options=BlendedOptions(blend_weight=200.0))
        self.assertEqual(result.status, "optimal")
        # Blended coefficient on x2 is now 100 - 200 = -100 (negative!)
        # -- the LP now WANTS x2 as large as possible, the real price
        # signal completely inverted by an unsafe hand-picked weight.
        self.assertAlmostEqual(p.value_of(result, "x2"), 10.0, places=4)
        self.assertAlmostEqual(p.value_of(result, "x1"), 0.0, places=4)


class TestCalibratedOptionsFindsASafeWeightAutomatically(unittest.TestCase):
    def test_breaks_the_genuine_tie_like_lex_does(self):
        p = _genuine_tie_problem()
        result = p.solve(options=CalibratedOptions())
        self.assertEqual(result.status, "optimal")
        # A searched-safe weight should still meaningfully move x2
        # toward its lex-optimal extreme (10.0) -- not necessarily
        # bit-identical to pure Lex (it's a blended solve at a found
        # weight, not a hard-constrained phase), but clearly in that
        # direction, not left at an arbitrary tied vertex.
        self.assertGreater(p.value_of(result, "x2"), 5.0)

    def test_never_meaningfully_degrades_a_real_price_difference(self):
        """The one guarantee CalibratedOptions makes that plain
        BlendedOptions (previous test) does NOT: whatever weight it
        finds, the real primary cost stays within its own
        calibration_tolerance of the true lex optimum -- automatically,
        without a human having to hand-verify the magnitude."""
        p = _real_price_difference_problem()
        result = p.solve(options=CalibratedOptions())
        self.assertEqual(result.status, "optimal")
        # True lex-optimal primary cost here is 10.0 (x1=10, x2=0) --
        # the calibrated weight must keep the ACHIEVED primary cost
        # (x1 + 100*x2, NOT the blended objective) within a small
        # tolerance of that, never anywhere close to the 200.0-weight
        # inversion the previous test class deliberately demonstrated.
        real_primary_cost = p.value_of(result, "x1") + 100.0 * p.value_of(result, "x2")
        self.assertLess(real_primary_cost, 10.1)


class TestSolveOptionsRejectsMip(unittest.TestCase):
    def test_options_on_a_mip_raises_not_implemented(self):
        p = LPProblem()
        p.add_variable("b", binary=True, cost=1.0)
        p.add_variable("x", ub=5.0, cost=1.0)
        p.set_secondary_cost("x", -1.0)
        with self.assertRaises(NotImplementedError):
            p.solve(options=LexOptions())

    def test_mip_without_options_is_completely_unaffected(self):
        """Regression guard: the is_mip check added for #696 must never
        fire on the ordinary options=None path -- every pre-#696 MIP
        caller keeps working exactly as before."""
        p = LPProblem()
        p.add_variable("b", binary=True, cost=1.0)
        p.add_variable("x", ub=5.0, cost=1.0)
        result = p.solve()
        self.assertEqual(result.status, "optimal")


class TestAllZeroPrimaryOrSecondaryIsAHonestNoOp(unittest.TestCase):
    def test_all_zero_primary_still_lets_secondary_pick_a_point(self):
        """Nimbus's own design choice, deliberately different from
        HAEO's (which raises if primary/secondary is entirely absent):
        an all-zero primary cost is a legitimate "I only care about the
        tie-break" scenario, not an error."""
        p = LPProblem()
        p.add_variable("x1", ub=10.0)
        p.add_variable("x2", ub=10.0)
        p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
        p.set_secondary_cost("x2", -1.0)
        result = p.solve(options=LexOptions())
        self.assertEqual(result.status, "optimal")
        self.assertAlmostEqual(p.value_of(result, "x2"), 10.0, places=4)

    def test_all_zero_secondary_is_a_clean_no_op(self):
        """No secondary cost set at all, but options= given anyway --
        phase 2 trivially minimizes a constant zero, changing nothing;
        the result must equal the true primary optimum exactly. A
        fresh problem, deliberately never calling set_secondary_cost()
        at all (unlike _real_price_difference_problem()'s own helper)."""
        p2 = LPProblem()
        p2.add_variable("x1", ub=10.0, cost=1.0)
        p2.add_variable("x2", ub=10.0, cost=100.0)
        p2.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
        result = p2.solve(options=LexOptions())
        self.assertEqual(result.status, "optimal")
        self.assertAlmostEqual(p2.value_of(result, "x1"), 10.0, places=4)
        self.assertAlmostEqual(p2.value_of(result, "x2"), 0.0, places=4)


if __name__ == "__main__":
    unittest.main()
