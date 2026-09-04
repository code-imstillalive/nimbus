"""Tests for reference_benchmark.py (nimbus issue #273, item #3).

Deliberately does NOT assert an exact dollar value for j_star/j_forecast/
etc. -- per that module's own docstring, this benchmark is a number to
WATCH release-over-release, not a hard-fail CI gate. A future genuine
Solver improvement is expected to legitimately move these numbers; an
exact-value regression test would just become an annoying, meaningless
"update the expected constant" chore on every real improvement. What
IS tested: the scenario is genuinely deterministic, and it satisfies
the same structural invariants every other regret/EPR tool in this
package already requires (oracle never beaten, non-negative regret).
"""

import unittest

import _solver_path  # noqa: F401
from solver.reference_benchmark import (
    REFERENCE_HOUSEHOLD_VERSION,
    build_reference_scenario,
    run_reference_benchmark,
)


class TestReferenceBenchmarkDeterminism(unittest.TestCase):
    def test_running_twice_gives_byte_identical_results(self):
        first = run_reference_benchmark()
        second = run_reference_benchmark()
        self.assertEqual(first.regret.j_star, second.regret.j_star)
        self.assertEqual(first.regret.j_forecast, second.regret.j_forecast)
        self.assertEqual(first.regret.j_persistence, second.regret.j_persistence)

    def test_scenario_curves_are_deterministic(self):
        first = build_reference_scenario()
        second = build_reference_scenario()
        for key in (
            "solar_real_kw",
            "load_real_kw",
            "solar_forecast_kw",
            "load_forecast_kw",
        ):
            self.assertTrue(
                (first[key] == second[key]).all(),
                f"{key} differed between two builds of the same scenario",
            )


class TestReferenceBenchmarkInvariants(unittest.TestCase):
    def setUp(self):
        self.result = run_reference_benchmark()

    def test_scenario_version_is_stamped(self):
        self.assertEqual(self.result.scenario_version, REFERENCE_HOUSEHOLD_VERSION)

    def test_oracle_is_never_beaten(self):
        r = self.result.regret
        self.assertLessEqual(r.j_star, r.j_forecast + 1e-6)
        self.assertLessEqual(r.j_star, r.j_persistence + 1e-6)

    def test_regrets_are_non_negative(self):
        r = self.result.regret
        self.assertGreaterEqual(r.forecast_regret_dollars, -1e-6)
        self.assertGreaterEqual(r.persistence_regret_dollars, -1e-6)

    def test_forecast_beats_naive_persistence_on_this_scenario(self):
        # The scenario's own forecast-error mechanism is deliberately a
        # smaller, unbiased perturbation than the persistence baseline's
        # fixed lag+scale error (see reference_benchmark.py's own
        # _reference_forecast_curves() docstring) -- a healthy pipeline
        # should show Nimbus's own forecast genuinely beating naive
        # persistence on this fixed scenario. If a future, real change
        # makes this flip negative, that is exactly the kind of signal
        # this benchmark exists to surface -- don't "fix" this test by
        # loosening the assertion without first understanding why it
        # flipped.
        self.assertGreater(self.result.nimbus_value_add_dollars, 0.0)

    def test_result_values_are_finite(self):
        import math

        r = self.result.regret
        for value in (r.j_star, r.j_forecast, r.j_persistence):
            self.assertTrue(math.isfinite(value))
