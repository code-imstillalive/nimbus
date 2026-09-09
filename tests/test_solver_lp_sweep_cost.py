"""Direct test coverage for lp.py's cost-sweep support (nimbus issue #494,
Signals 5/7 of #489 -- "offer curve: parametric price sweep giving
(price, kW) import/export steps"). Opt-in via
`LPProblem.solve(keep_basis=True)`, then `LPResult.sweep_cost(var, costs)`.

The main scenario below is a small, hand-worked arbitrage LP:

    min p*imp - 0.05*exp
    s.t. imp - exp + solar = 2
         solar <= 6
         imp, exp, solar >= 0   (imp, exp additionally capped at 100)

Solved once at p=0.30 (the "retail" import price), then swept across a
grid of import costs -- every value below was confirmed directly against
a live highspy install before writing these assertions, not guessed.
"""

from __future__ import annotations

import unittest
from itertools import pairwise

import _solver_path  # noqa: F401
from solver.lp import LPProblem


def _arbitrage_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("imp", ub=100.0, cost=0.30)
    p.add_variable("exp", ub=100.0, cost=-0.05)
    p.add_variable("solar", ub=6.0)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, 2.0, name="balance")
    return p


class TestSweepCostRequiresKeepBasis(unittest.TestCase):
    def test_raises_without_keep_basis(self):
        p = _arbitrage_problem()
        result = p.solve()  # keep_basis defaults to False
        with self.assertRaises(ValueError):
            result.sweep_cost("imp", [0.30])

    def test_keep_basis_false_leaves_highs_none(self):
        p = _arbitrage_problem()
        result = p.solve(keep_basis=False)
        self.assertIsNone(result._highs)
        self.assertEqual(result._var_index, {})

    def test_keep_basis_true_retains_a_live_highs_instance(self):
        p = _arbitrage_problem()
        result = p.solve(keep_basis=True)
        self.assertIsNotNone(result._highs)
        self.assertIn("imp", result._var_index)


class TestSweepCostHandWorkedValues(unittest.TestCase):
    def setUp(self):
        self.p = _arbitrage_problem()
        self.result = self.p.solve(keep_basis=True)

    def test_base_solve_matches_expectation(self):
        self.assertEqual(self.result.status, "optimal")
        self.assertAlmostEqual(self.p.value_of(self.result, "imp"), 0.0)
        self.assertAlmostEqual(self.p.value_of(self.result, "exp"), 4.0)

    def test_sweep_matches_hand_worked_values(self):
        prices = [-1.0, -0.9, -0.3, 0.0, 0.30, 0.9, 20.0]
        values = self.result.sweep_cost("imp", prices)
        expected = [100.0, 100.0, 100.0, 100.0, 0.0, 0.0, 0.0]
        for got, want in zip(values, expected, strict=True):
            self.assertAlmostEqual(got, want)

    def test_sweep_is_non_increasing_as_price_rises(self):
        prices = [-1.0, -0.9, -0.3, 0.0, 0.30, 0.9, 20.0]
        values = self.result.sweep_cost("imp", prices)
        for earlier, later in pairwise(values):
            self.assertGreaterEqual(earlier, later - 1e-9)

    def test_sweep_at_the_original_cost_reproduces_the_base_solve(self):
        # Consistency check nimbus issue #494 itself calls for: "curve at
        # the current retail price equals the main plan's period-0
        # import/export".
        (value,) = self.result.sweep_cost("imp", [0.30])
        self.assertAlmostEqual(value, self.p.value_of(self.result, "imp"))

    def test_sweep_restores_the_original_cost_coefficient_afterward(self):
        original = self.p.value_of(self.result, "imp")
        self.result.sweep_cost("imp", [20.0, -1.0, 5.0])
        # A second sweep, at exactly the original cost, must reproduce
        # the original value -- if the first sweep hadn't restored the
        # coefficient afterward, this would instead start from whatever
        # the last swept value (5.0) left behind.
        (restored,) = self.result.sweep_cost("imp", [0.30])
        self.assertAlmostEqual(restored, original)

    def test_sweeping_export_is_independent_of_the_import_sweep(self):
        # network.py's own real usage: sweep import, then export, off
        # the SAME LPResult -- neither leaves the other's column
        # mutated.
        self.result.sweep_cost("imp", [20.0, -1.0])
        export_values = self.result.sweep_cost("exp", [-0.05])
        self.assertAlmostEqual(export_values[0], 4.0)
        # imp's own coefficient was restored by its own sweep, so a
        # plain re-check at its original cost still matches.
        (imp_check,) = self.result.sweep_cost("imp", [0.30])
        self.assertAlmostEqual(imp_check, 0.0)


class TestSweepCostErrors(unittest.TestCase):
    def test_unknown_variable_raises_keyerror(self):
        p = _arbitrage_problem()
        result = p.solve(keep_basis=True)
        with self.assertRaises(KeyError):
            result.sweep_cost("nonexistent", [0.30])


if __name__ == "__main__":
    unittest.main()
