"""Direct test coverage for lp.py's HiGHS ranging support (nimbus issue
#490, Signals 1/7 of #489 -- "grid-operator and load-ranging signals
from HiGHS ranging"). Opt-in via `LPProblem.solve(ranging=True)`.

The main scenario below is the exact probe LP from #489's own research
doc, so every asserted number is checkable by hand, not just "did it not
crash":

    min 0.30*imp + 0.05*exp
    s.t. imp - exp + solar = 2
         solar <= 6
         imp, exp, solar >= 0

Confirmed directly against a live highspy install before writing these
assertions (not guessed from the HiGHS docs alone) -- including the real,
undocumented quirk that col_cost_up/col_cost_dn come back ONE ELEMENT
LONGER than the real column count (see lp.py's own `_build_ranging_dict`
docstring).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import highspy
from solver.lp import LPProblem


def _probe_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("imp", cost=0.30)
    p.add_variable("exp", cost=0.05)
    p.add_variable("solar", ub=6.0)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, 2.0, name="balance")
    return p


class TestProbeLPRangingValues(unittest.TestCase):
    def setUp(self):
        self.p = _probe_problem()
        self.result = self.p.solve(ranging=True)

    def test_solves_to_the_expected_point(self):
        self.assertEqual(self.result.status, "optimal")
        self.assertAlmostEqual(self.p.value_of(self.result, "imp"), 0.0)
        self.assertAlmostEqual(self.p.value_of(self.result, "exp"), 0.0)
        self.assertAlmostEqual(self.p.value_of(self.result, "solar"), 2.0)

    def test_reduced_costs_match_the_objective_coefficients(self):
        self.assertAlmostEqual(self.result.reduced_costs["imp"], 0.30)
        self.assertAlmostEqual(self.result.reduced_costs["exp"], 0.05)
        self.assertAlmostEqual(self.result.reduced_costs["solar"], 0.0)

    def test_ranging_valid_is_true(self):
        self.assertTrue(self.result.ranging_valid)

    def test_col_bound_up_matches_the_hand_worked_values(self):
        self.assertAlmostEqual(self.result.col_bound_up["imp"].value, 2.0)
        self.assertAlmostEqual(self.result.col_bound_up["exp"].value, 4.0)
        self.assertAlmostEqual(self.result.col_bound_up["solar"].value, 6.0)

    def test_col_bound_dn_matches_the_hand_worked_values(self):
        self.assertAlmostEqual(self.result.col_bound_dn["imp"].value, -4.0)
        self.assertAlmostEqual(self.result.col_bound_dn["exp"].value, -2.0)
        self.assertAlmostEqual(self.result.col_bound_dn["solar"].value, 0.0)

    def test_col_cost_ranging_for_solar_matches_the_hand_worked_values(self):
        # solar's own objective coefficient is 0.0 (never priced) -- this
        # is how far that coefficient could move before the basis
        # changes, not solar's own value.
        self.assertAlmostEqual(self.result.col_cost_up["solar"].value, 0.30)
        self.assertAlmostEqual(self.result.col_cost_dn["solar"].value, -0.05)

    def test_col_cost_dicts_are_exactly_one_entry_per_real_variable(self):
        # nimbus issue #490: highspy's own col_cost_up/dn come back one
        # element longer than the real column count -- _build_ranging_
        # dict() must drop the trailing extra, never attribute it to a
        # variable that doesn't exist.
        self.assertEqual(set(self.result.col_cost_up), {"imp", "exp", "solar"})
        self.assertEqual(set(self.result.col_cost_dn), {"imp", "exp", "solar"})

    def test_row_bound_ranging_matches_the_hand_worked_values(self):
        self.assertAlmostEqual(self.result.row_bound_up["balance"].value, 6.0)
        self.assertAlmostEqual(self.result.row_bound_dn["balance"].value, 0.0)


class TestRangingNotRequested(unittest.TestCase):
    def test_ranging_valid_is_none_and_dicts_are_empty(self):
        p = _probe_problem()
        result = p.solve()  # ranging defaults to False
        self.assertIsNone(result.ranging_valid)
        self.assertEqual(result.col_bound_up, {})
        self.assertEqual(result.col_bound_dn, {})
        self.assertEqual(result.col_cost_up, {})
        self.assertEqual(result.col_cost_dn, {})
        self.assertEqual(result.row_bound_up, {})
        self.assertEqual(result.row_bound_dn, {})

    def test_getranging_is_never_called_when_not_requested(self):
        # Solve time must be unchanged for the overwhelming majority of
        # callers that never ask for ranging -- proven here by spying on
        # the real Highs.getRanging method, not just "the result looked
        # empty" (which could also happen from a silently-broken call).
        # A plain function (not unittest.mock.MagicMock/wraps=) is used
        # to patch it -- confirmed live that MagicMock's own call
        # protocol doesn't correctly forward pybind11's own `self`
        # argument for this bound method, raising a spurious TypeError;
        # a plain function assigned as a class attribute is a real
        # descriptor, so instance-method self-binding still works.
        p = _probe_problem()
        calls: list[None] = []
        original = highspy.Highs.getRanging

        def _tracking(self_obj):
            calls.append(None)
            return original(self_obj)

        with patch.object(highspy.Highs, "getRanging", _tracking):
            p.solve()
        self.assertEqual(calls, [])

    def test_getranging_is_called_exactly_once_when_requested(self):
        p = _probe_problem()
        calls: list[None] = []
        original = highspy.Highs.getRanging

        def _tracking(self_obj):
            calls.append(None)
            return original(self_obj)

        with patch.object(highspy.Highs, "getRanging", _tracking):
            p.solve(ranging=True)
        self.assertEqual(len(calls), 1)


class TestRangingOnNonOptimalStatus(unittest.TestCase):
    def test_infeasible_reports_ranging_valid_false_with_empty_dicts(self):
        p = LPProblem()
        p.add_variable("x", ub=1.0)
        p.add_eq_constraint({"x": 1.0}, 5.0)  # x <= 1 but forced to 5
        result = p.solve(ranging=True)
        self.assertEqual(result.status, "infeasible")
        self.assertFalse(result.ranging_valid)
        self.assertEqual(result.col_bound_up, {})
        self.assertEqual(result.row_bound_up, {})

    def test_infeasible_without_ranging_requested_stays_none(self):
        p = LPProblem()
        p.add_variable("x", ub=1.0)
        p.add_eq_constraint({"x": 1.0}, 5.0)
        result = p.solve()
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.ranging_valid)


class TestRangingOnMip(unittest.TestCase):
    def test_binary_problem_ranging_valid_after_the_pinned_pass(self):
        # A tiny MIP: y in {0, 1} gates how much of x can be produced.
        # Ranging must reflect the PINNED LP (y fixed at its solved
        # value), not the raw branch-and-bound tree -- same real
        # requirement duals already have on a MIP (nimbus issue #238).
        p = LPProblem()
        y = p.add_variable("y", binary=True)
        x = p.add_variable("x", ub=10.0, cost=-1.0)
        p.add_ub_constraint({x: 1.0, y: -10.0}, 0.0, name="gate")
        result = p.solve(ranging=True)
        self.assertEqual(result.status, "optimal")
        self.assertTrue(result.ranging_valid)
        self.assertIn("x", result.col_bound_up)
        self.assertIn("y", result.col_bound_up)


class TestHeadroomHelpers(unittest.TestCase):
    def setUp(self):
        self.p = _probe_problem()
        self.result = self.p.solve(ranging=True)

    def test_bound_headroom_matches_col_bound_minus_x(self):
        # solar = 2.0, col_bound_dn = 0.0, col_bound_up = 6.0.
        headroom = self.result.bound_headroom("solar")
        self.assertIsNotNone(headroom)
        self.assertAlmostEqual(headroom.down, 2.0)  # 2.0 - 0.0
        self.assertAlmostEqual(headroom.up, 4.0)  # 6.0 - 2.0
        self.assertFalse(headroom.degenerate)

    def test_bound_headroom_at_x_equal_zero_is_degenerate_on_the_down_side(self):
        # imp = 0.0 (sitting at its own lb), col_bound_dn = -4.0 -- the
        # DOWN side (imp - col_bound_dn) is a real 4.0, not degenerate;
        # check a genuinely zero-width case instead using solar pinned
        # to its own bound.
        p = LPProblem()
        p.add_variable("imp", cost=0.30)
        p.add_variable("exp", cost=0.05)
        solar = p.add_variable("solar", lb=6.0, ub=6.0)  # pinned, zero-width
        p.add_eq_constraint({"imp": 1.0, "exp": -1.0, solar: 1.0}, 6.0, name="balance")
        result = p.solve(ranging=True)
        headroom = result.bound_headroom("solar")
        self.assertIsNotNone(headroom)
        self.assertTrue(headroom.degenerate)
        self.assertEqual(headroom.down, 0.0)
        self.assertEqual(headroom.up, 0.0)

    def test_rhs_headroom_matches_row_bound_minus_row_value(self):
        # The equality row's own achieved value is exactly its rhs (2.0);
        # row_bound_dn=0.0, row_bound_up=6.0.
        headroom = self.result.rhs_headroom("balance")
        self.assertIsNotNone(headroom)
        self.assertAlmostEqual(headroom.down, 2.0)
        self.assertAlmostEqual(headroom.up, 4.0)

    def test_headroom_helpers_return_none_when_ranging_not_available(self):
        p = _probe_problem()
        result = p.solve()  # no ranging
        self.assertIsNone(result.bound_headroom("solar"))
        self.assertIsNone(result.rhs_headroom("balance"))

    def test_headroom_helpers_return_none_for_an_unknown_name(self):
        self.assertIsNone(self.result.bound_headroom("does_not_exist"))
        self.assertIsNone(self.result.rhs_headroom("does_not_exist"))


if __name__ == "__main__":
    unittest.main()
