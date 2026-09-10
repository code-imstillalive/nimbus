"""Direct test coverage for lp.py's ranging-annotated cost sweep (nimbus
issue #676 -- "offer curve: publish ranging at every sweep point, a
comprehensive exact-breakpoint offer curve, not one breakeven price").
`LPResult.sweep_cost_with_ranging(var, costs)`, opt-in via
`LPProblem.solve(keep_basis=True)` same as plain `sweep_cost()`.

Same hand-worked arbitrage LP as test_solver_lp_sweep_cost.py:

    min p*imp - 0.05*exp
    s.t. imp - exp + solar = 2
         solar <= 6
         imp, exp, solar >= 0   (imp, exp additionally capped at 100)

Every ranging value below was confirmed directly against a live highspy
install first (`result.sweep_cost_with_ranging("imp", [...])`, printed
and inspected), not guessed -- #676's own open question ("has this
repeated-getRanging()-on-a-re-solved-instance pattern ever been tried")
is answered here: yes, it returns fresh, correct, real values at every
step, not stale/cached state from the original solve.
"""

from __future__ import annotations

import math
import unittest

import _solver_path  # noqa: F401
from solver.lp import LPProblem


def _arbitrage_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("imp", ub=100.0, cost=0.30)
    p.add_variable("exp", ub=100.0, cost=-0.05)
    p.add_variable("solar", ub=6.0)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, 2.0, name="balance")
    return p


class TestSweepCostWithRangingRequiresKeepBasis(unittest.TestCase):
    def test_raises_without_keep_basis(self):
        p = _arbitrage_problem()
        result = p.solve()  # keep_basis defaults to False
        with self.assertRaises(ValueError):
            result.sweep_cost_with_ranging("imp", [0.30])

    def test_unknown_variable_raises_keyerror(self):
        p = _arbitrage_problem()
        result = p.solve(keep_basis=True)
        with self.assertRaises(KeyError):
            result.sweep_cost_with_ranging("nonexistent", [0.30])


class TestSweepCostWithRangingHandWorkedValues(unittest.TestCase):
    def setUp(self):
        self.p = _arbitrage_problem()
        self.result = self.p.solve(keep_basis=True)

    def test_values_match_plain_sweep_cost(self):
        # sweep_cost_with_ranging() must report the exact same kW values
        # as plain sweep_cost() -- the ranging annotation is additive,
        # never a different re-solve path.
        prices = [-1.0, -0.9, -0.3, 0.0, 0.30, 0.9, 20.0]
        plain_values = self.p.solve(keep_basis=True).sweep_cost("imp", prices)
        steps = self.result.sweep_cost_with_ranging("imp", prices)
        for plain, step in zip(plain_values, steps, strict=True):
            self.assertAlmostEqual(plain, step.value)

    def test_low_price_plateau_ranging_is_exact(self):
        # imp pinned at its own 100.0 upper bound for any cost below the
        # real breakeven (0.05, where importing becomes exactly as
        # attractive as the export alternative it displaces) -- ranging
        # reports this exact plateau: unbounded below, exactly 0.0 above.
        # Real, confirmed values (not guessed): cost_dn=-inf, cost_up=0.0
        # at every one of these three deep-plateau points.
        steps = self.result.sweep_cost_with_ranging("imp", [-1.0, -0.9, -0.3])
        for step in steps:
            self.assertAlmostEqual(step.value, 100.0)
            self.assertIsNotNone(step.cost_dn)
            self.assertIsNotNone(step.cost_up)
            self.assertTrue(math.isinf(step.cost_dn.value) and step.cost_dn.value < 0)
            self.assertAlmostEqual(step.cost_up.value, 0.0, places=6)

    def test_high_price_plateau_ranging_is_exact(self):
        # imp pinned at its own 0.0 lower bound for any cost above the
        # same 0.05 breakeven -- unbounded above, exactly 0.05 below.
        steps = self.result.sweep_cost_with_ranging("imp", [0.30, 0.9, 20.0])
        for step in steps:
            self.assertAlmostEqual(step.value, 0.0)
            self.assertIsNotNone(step.cost_dn)
            self.assertIsNotNone(step.cost_up)
            self.assertAlmostEqual(step.cost_dn.value, 0.05, places=6)
            self.assertTrue(math.isinf(step.cost_up.value) and step.cost_up.value > 0)

    def test_ranging_entry_and_exit_variables_are_named(self):
        # Real values confirmed live: at cost=0.30, imp's own upper
        # ranging bound is a genuine "the problem becomes unbounded in
        # that direction" case (no entering/leaving variable at all,
        # both None); the lower bound names exp as what would enter the
        # basis if imp's cost fell below 0.05.
        (step,) = self.result.sweep_cost_with_ranging("imp", [0.30])
        self.assertEqual(step.cost_dn.in_var, "imp")
        self.assertEqual(step.cost_dn.out_var, "exp")
        self.assertIsNone(step.cost_up.in_var)
        self.assertIsNone(step.cost_up.out_var)

    def test_restores_the_original_cost_coefficient_afterward(self):
        original = self.p.value_of(self.result, "imp")
        self.result.sweep_cost_with_ranging("imp", [20.0, -1.0, 5.0])
        (restored,) = self.result.sweep_cost("imp", [0.30])
        self.assertAlmostEqual(restored, original)

    def test_sweeping_export_with_ranging_is_independent_of_import(self):
        self.result.sweep_cost_with_ranging("imp", [20.0, -1.0])
        (export_step,) = self.result.sweep_cost_with_ranging("exp", [-0.05])
        self.assertAlmostEqual(export_step.value, 4.0)
        (imp_check,) = self.result.sweep_cost("imp", [0.30])
        self.assertAlmostEqual(imp_check, 0.0)

    def test_swept_cost_always_lies_within_its_own_reported_interval(self):
        # General invariant, real values or not: whatever cost coefficient
        # was actually swept to must lie within [cost_dn, cost_up] -- that
        # is the entire meaning of "this is the valid range for this
        # value". Checked across a real mix of plateau and interior points.
        prices = [-1.0, -0.3, 0.0, 0.05, 0.06, 0.30, 20.0]
        steps = self.result.sweep_cost_with_ranging("imp", prices)
        for cost, step in zip(prices, steps, strict=True):
            self.assertIsNotNone(step.cost_dn)
            self.assertIsNotNone(step.cost_up)
            self.assertLessEqual(step.cost_dn.value, cost + 1e-9)
            self.assertGreaterEqual(step.cost_up.value, cost - 1e-9)


if __name__ == "__main__":
    unittest.main()
