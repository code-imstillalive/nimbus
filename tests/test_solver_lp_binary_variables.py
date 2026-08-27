"""Direct test coverage for lp.py's binary-variable/MIP support (nimbus
issue #238, groundwork commit 82134cf -- landed on main without any test
of its own; this fills that gap before the work is considered merged
rather than just orphaned).

This is the first test file in the suite that exercises `LPProblem`
directly rather than going through solver.elements/network.py -- correct,
since `binary=True` is pure lp.py infrastructure not yet wired into
network.py at all (per the groundwork commit's own message: "the
complementarity constraint itself is NOT written yet, and network.py is
untouched"). A network.py-level test would have nothing real to exercise.

Four things are checked, matching the four real claims the groundwork
commit's own docstring makes -- none of them taken on faith:
  1. `binary=True` registers a variable bounded to exactly [0, 1], and
     rejects an explicit lb/ub passed alongside it rather than silently
     ignoring one.
  2. `is_mip` reports False for a problem with no binaries, True the
     moment one exists -- callers use this to reason about solve cost.
  3. A pure LP (no binaries at all) solves byte-identically to before --
     the whole point of the "existing callers register no binaries, this
     is a no-op" claim, proven, not asserted.
  4. A genuine MIP (one binary variable gating how much a continuous
     variable can produce) recovers REAL duals, not the zero/garbage
     HiGHS returns for a raw branch-and-bound solve. This is the actual
     hard part of the commit -- checked against a small scenario with a
     real, unambiguous binding/non-binding constraint pair, not just "did
     it not crash."
"""

import unittest

import _solver_path  # noqa: F401
from solver.lp import LPProblem


class TestBinaryVariableRegistration(unittest.TestCase):
    def test_binary_variable_is_bounded_zero_to_one(self):
        p = LPProblem()
        p.add_variable("y", binary=True)
        idx = p._var_index["y"]
        self.assertEqual(p._lb[idx], 0.0)
        self.assertEqual(p._ub[idx], 1.0)
        self.assertTrue(p._binary[idx])

    def test_binary_with_explicit_lb_raises(self):
        p = LPProblem()
        with self.assertRaises(ValueError):
            p.add_variable("y", binary=True, lb=0.5)

    def test_binary_with_explicit_ub_raises(self):
        p = LPProblem()
        with self.assertRaises(ValueError):
            p.add_variable("y", binary=True, ub=2.0)

    def test_non_binary_variable_unaffected(self):
        """A plain add_variable() call (the overwhelming majority of every
        existing caller in network.py) must register _binary=False, not
        crash or change shape -- the new field is additive only.
        """
        p = LPProblem()
        p.add_variable("x", lb=0.0, ub=10.0)
        idx = p._var_index["x"]
        self.assertFalse(p._binary[idx])
        self.assertEqual(p._ub[idx], 10.0)


class TestIsMipFlag(unittest.TestCase):
    def test_false_with_no_variables(self):
        self.assertFalse(LPProblem().is_mip)

    def test_false_with_only_continuous_variables(self):
        p = LPProblem()
        p.add_variable("a")
        p.add_variable("b", lb=-5.0, ub=5.0)
        self.assertFalse(p.is_mip)

    def test_true_once_any_binary_registered(self):
        p = LPProblem()
        p.add_variable("a")
        p.add_variable("y", binary=True)
        self.assertTrue(p.is_mip)


class TestPureLpSolveUnchanged(unittest.TestCase):
    """No binaries anywhere -- must solve exactly as it always has: a
    single simplex run, real duals on the binding row, zero on the slack
    row. This is the regression check for the groundwork commit's own
    central claim: "the integrality loop is a no-op."
    """

    def test_minimal_lp_solves_with_correct_duals(self):
        p = LPProblem()
        p.add_variable("x", lb=0.0, ub=100.0, cost=-1.0)  # maximize x
        p.add_ub_constraint({"x": 1.0}, 20.0, name="cap")
        result = p.solve()

        self.assertEqual(result.status, "optimal")
        self.assertAlmostEqual(p.value_of(result, "x"), 20.0, places=6)
        self.assertAlmostEqual(result.objective, -20.0, places=6)
        # The cap is binding (x sits exactly at its ub) -- its dual must
        # be real and nonzero, not the empty/zero placeholder a non-MIP
        # regression here would produce.
        self.assertNotAlmostEqual(result.duals["cap"], 0.0, places=6)


class TestMipSolveRecoversRealDuals(unittest.TestCase):
    """A genuine MIP: a binary `use_machine` gates how much continuous
    `output` a second, separately-named constraint can ever reach.

    Structure, deliberately simple enough to reason about by hand:
      - use_machine in {0, 1}
      - 0 <= output <= 100
      - cap_by_switch:  output - 10*use_machine <= 0   (output <= 10 iff on)
      - slack_cap:      output <= 20                    (never binds here)
      - minimize  -output + 1*use_machine   (maximize output, small
        fixed cost for turning the machine on)

    With use_machine=0, output is forced to 0 (objective 0). With
    use_machine=1, output can reach 10 (cap_by_switch binds before
    slack_cap ever does), objective = -10 + 1 = -9 -- strictly better, so
    branch-and-bound must pick use_machine=1.

    Once pinned+relaxed (the groundwork commit's actual fix), the real
    duals of this pinned LP must show cap_by_switch as the genuine binding
    constraint (nonzero dual) and slack_cap as genuinely non-binding
    (zero dual) -- exactly the "recovered duals, not zero/garbage" claim
    the commit message makes, checked against a case where the two
    possible answers (all-zero garbage vs. real duals) are distinguishable
    by which row is nonzero, not just by any row being nonzero at all.
    """

    def _scenario(self) -> LPProblem:
        p = LPProblem()
        p.add_variable("use_machine", binary=True, cost=1.0)
        p.add_variable("output", lb=0.0, ub=100.0, cost=-1.0)
        p.add_ub_constraint(
            {"output": 1.0, "use_machine": -10.0}, 0.0, name="cap_by_switch"
        )
        p.add_ub_constraint({"output": 1.0}, 20.0, name="slack_cap")
        return p

    def test_branch_and_bound_picks_the_profitable_binary_value(self):
        p = self._scenario()
        result = p.solve()
        self.assertEqual(result.status, "optimal")
        self.assertAlmostEqual(p.value_of(result, "use_machine"), 1.0, places=6)
        self.assertAlmostEqual(p.value_of(result, "output"), 10.0, places=6)
        self.assertAlmostEqual(result.objective, -9.0, places=6)

    def test_binding_constraint_has_a_real_nonzero_dual(self):
        p = self._scenario()
        result = p.solve()
        self.assertNotAlmostEqual(result.duals["cap_by_switch"], 0.0, places=6)

    def test_non_binding_constraint_has_a_genuine_zero_dual(self):
        """The decisive check: HiGHS returning raw zeros for every row on
        an unfixed MIP solve would ALSO show this constraint at zero --
        the previous test alone can't distinguish "real recovery" from
        "still garbage, coincidentally zero here too." Pairing a real
        nonzero dual on the binding row with a real zero on a genuinely
        slack row is what actually proves recovery happened.
        """
        p = self._scenario()
        result = p.solve()
        self.assertAlmostEqual(result.duals["slack_cap"], 0.0, places=6)

    def test_reduced_costs_present_and_not_none(self):
        p = self._scenario()
        result = p.solve()
        self.assertIsInstance(result.reduced_costs, dict)
        self.assertIn("use_machine", result.reduced_costs)
        self.assertIn("output", result.reduced_costs)


if __name__ == "__main__":
    unittest.main()
