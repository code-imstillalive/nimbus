"""ml/blend.py -- multi-source forecast blending, Track A0 of the Nimbus
Solver stochastic/blended forecasting plan (2026-08-21). See that
module's own docstring for the full "every forecast is wrong, so blend
rather than pick one" reasoning.

Pure math, zero HA dependencies, tested completely independent of
however this eventually gets wired into a live config flow (a genuinely
separate, not-yet-scoped piece -- see this project's own CLAUDE.md
session log for why that wiring was deliberately deferred rather than
rushed the same night).
"""
import unittest

import _solver_path  # noqa: F401 -- adds custom_components/nimbus_load to sys.path
import numpy as np
from ml.blend import blend_forecast_array, blend_point_estimate, cross_source_spread, weights_from_mae


class TestWeightsFromMae(unittest.TestCase):
    def test_all_unknown_returns_equal_weights(self):
        """The honest bootstrap case -- no real accuracy data yet for
        any configured source (this project's own real state as of
        2026-08-21: forecast-capture had only one snapshot on disk)."""
        w = weights_from_mae([None, None, None])
        self.assertEqual(len(w), 3)
        for x in w:
            self.assertAlmostEqual(x, 1.0 / 3.0)
        self.assertAlmostEqual(sum(w), 1.0)

    def test_half_mae_gets_roughly_double_weight(self):
        """Source A has half the error of source B -> should get
        roughly twice the weight, the direct definition of
        inverse-error weighting."""
        w = weights_from_mae([1.0, 2.0])
        self.assertAlmostEqual(w[0] / w[1], 2.0, places=6)
        self.assertAlmostEqual(sum(w), 1.0)

    def test_unknown_source_gets_same_weight_as_worst_known(self):
        """Mixed known/unknown: the unknown source should be treated as
        no better than the worst KNOWN source, not silently dropped to
        zero (dropping it would make "blend" secretly mean "ignore
        anything unmeasured", defeating the whole point of blending
        multiple sources in the first place)."""
        w = weights_from_mae([1.0, 5.0, None])
        # unknown (index 2) should get the same weight as the worst
        # known source (index 1, mae=5.0)
        self.assertAlmostEqual(w[2], w[1], places=6)
        self.assertGreater(w[0], w[1])  # the genuinely more accurate source still wins
        self.assertAlmostEqual(sum(w), 1.0)

    def test_zero_mae_treated_as_unknown_not_as_perfect(self):
        """A source claiming exactly zero real-world error is not
        something to trust infinitely -- must not divide by zero, and
        must not dominate the blend completely."""
        w = weights_from_mae([0.0, 2.0])
        self.assertTrue(all(np.isfinite(x) for x in w))
        self.assertLess(w[0], 1.0)  # not treated as "perfect, ignore everything else"

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            weights_from_mae([])


class TestBlendPointEstimate(unittest.TestCase):
    def test_equal_weight_default_is_plain_average(self):
        self.assertAlmostEqual(blend_point_estimate([10.0, 20.0]), 15.0)

    def test_explicit_weights_respected(self):
        # 3x weight on the first value -> pulls the blend toward it
        result = blend_point_estimate([10.0, 20.0], weights=[3.0, 1.0])
        self.assertAlmostEqual(result, (10.0 * 3.0 + 20.0 * 1.0) / 4.0)

    def test_single_source_returns_that_source_unchanged(self):
        self.assertAlmostEqual(blend_point_estimate([42.5]), 42.5)

    def test_mismatched_weights_length_raises(self):
        with self.assertRaises(ValueError):
            blend_point_estimate([1.0, 2.0], weights=[1.0])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            blend_point_estimate([])


class TestBlendForecastArray(unittest.TestCase):
    def test_equal_weight_elementwise_average(self):
        a = np.array([10.0, 20.0, 30.0])
        b = np.array([20.0, 30.0, 40.0])
        result = blend_forecast_array([a, b])
        np.testing.assert_allclose(result, [15.0, 25.0, 35.0])

    def test_weighted_blend_pulls_toward_higher_weight_source(self):
        a = np.array([0.0, 0.0])
        b = np.array([10.0, 10.0])
        result = blend_forecast_array([a, b], weights=[3.0, 1.0])
        np.testing.assert_allclose(result, [2.5, 2.5])

    def test_mismatched_array_lengths_raises(self):
        a = np.array([1.0, 2.0, 3.0])
        b = np.array([1.0, 2.0])
        with self.assertRaises(ValueError):
            blend_forecast_array([a, b])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            blend_forecast_array([])


class TestCrossSourceSpread(unittest.TestCase):
    def test_single_source_has_zero_spread(self):
        """A real, expected transient state -- blending is configured
        but only one source happens to be live right now -- must not
        error, must correctly report zero disagreement."""
        a = np.array([10.0, 20.0, 30.0])
        result = cross_source_spread([a])
        np.testing.assert_allclose(result, [0.0, 0.0, 0.0])

    def test_two_sources_spread_is_plain_max_minus_min(self):
        a = np.array([10.0, 5.0])
        b = np.array([12.0, 15.0])
        result = cross_source_spread([a, b])
        np.testing.assert_allclose(result, [2.0, 10.0])

    def test_three_sources_spread_uses_full_range_not_pairwise(self):
        a = np.array([10.0])
        b = np.array([15.0])
        c = np.array([8.0])
        result = cross_source_spread([a, b, c])
        np.testing.assert_allclose(result, [7.0])  # max(15) - min(8), not any single pairwise difference

    def test_identical_sources_have_zero_spread(self):
        a = np.array([10.0, 20.0])
        b = np.array([10.0, 20.0])
        result = cross_source_spread([a, b])
        np.testing.assert_allclose(result, [0.0, 0.0])

    def test_mismatched_lengths_raises(self):
        a = np.array([1.0, 2.0])
        b = np.array([1.0])
        with self.assertRaises(ValueError):
            cross_source_spread([a, b])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            cross_source_spread([])


if __name__ == "__main__":
    unittest.main()
