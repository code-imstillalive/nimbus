"""Regression test for nimbus issue #352 (Mark Purcell, codebase review):
calibrated_band()'s cold-start fallback (fewer than MIN_RESIDUALS_FOR_
CALIBRATION real residuals) multiplied point_value by COLD_START_BAND_
FRACTION directly -- for a negative point_value (a real, expected shape
for a power-signal prediction under allow_negative, e.g. "charging at
20kW") this produced a NEGATIVE half-width. coordinator.py's own
lower = v - band / upper = v + band then silently inverts (lower > upper)
for the first ~10 update cycles after install or a residual-file reset,
and for every k-NN/naive signal (which always uses this residual-based
band, never a model-derived one).

Real, from-scratch call into the actual calibrated_band() -- not a
reimplementation -- same "ml/model.py has zero homeassistant.* imports,
directly importable" convention as this project's other ml test files.
"""

from __future__ import annotations

import unittest

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model


class TestCalibratedBandColdStartSign(unittest.TestCase):
    def test_negative_point_value_yields_a_positive_band(self):
        band = ml_model.calibrated_band([], -20.0, 0.0)
        self.assertGreater(band, 0.0)

    def test_negative_point_value_band_magnitude_matches_positive_counterpart(self):
        negative_band = ml_model.calibrated_band([], -20.0, 0.0)
        positive_band = ml_model.calibrated_band([], 20.0, 0.0)
        self.assertAlmostEqual(negative_band, positive_band)

    def test_zero_point_value_still_gets_a_nonzero_band(self):
        band = ml_model.calibrated_band([], 0.0, 0.0)
        self.assertGreater(band, 0.0)
        self.assertAlmostEqual(band, ml_model.COLD_START_BAND_MIN_KW)

    def test_small_point_value_is_floored_not_left_near_zero(self):
        # 0.1kW * COLD_START_BAND_FRACTION (0.3) = 0.03, well under the floor.
        band = ml_model.calibrated_band([], 0.1, 0.0)
        self.assertGreaterEqual(band, ml_model.COLD_START_BAND_MIN_KW)

    def test_large_point_value_uses_the_fraction_not_the_floor(self):
        band = ml_model.calibrated_band([], -50.0, 0.0)
        self.assertAlmostEqual(band, 50.0 * ml_model.COLD_START_BAND_FRACTION)

    def test_lower_never_exceeds_upper_for_a_negative_prediction(self):
        point_value = -20.0
        band = ml_model.calibrated_band([], point_value, 0.0)
        lower = point_value - band
        upper = point_value + band
        self.assertLessEqual(lower, upper)


class TestCalibrationHalfWidthPrecomputation(unittest.TestCase):
    """Regression test for nimbus issue #366 finding 2: coordinator.py's
    per-horizon-point loop (~385 points/tick) used to call calibrated_band()
    with no way to avoid recomputing np.percentile(residuals, ...) on every
    single point even though residuals never changes within one cycle.
    calibration_half_width() lets a caller compute that once and pass it
    back in via calibrated_band()'s near_term_half_width override.
    """

    def test_returns_none_below_the_calibration_threshold(self):
        residuals = [1.0] * (ml_model.MIN_RESIDUALS_FOR_CALIBRATION - 1)
        self.assertIsNone(ml_model.calibration_half_width(residuals))

    def test_matches_calibrated_bands_own_internal_computation(self):
        residuals = [float(i % 7 + 1) for i in range(50)]
        precomputed = ml_model.calibration_half_width(residuals)
        self.assertIsNotNone(precomputed)

        for lead_hours in (0.0, 1.5, 24.0):
            band_internal = ml_model.calibrated_band(residuals, 10.0, lead_hours)
            band_precomputed = ml_model.calibrated_band(
                residuals, 10.0, lead_hours, near_term_half_width=precomputed
            )
            self.assertAlmostEqual(band_internal, band_precomputed, places=9)

    def test_override_is_ignored_below_the_calibration_threshold(self):
        # A caller that (incorrectly) passes a precomputed half-width
        # alongside too-few residuals still gets the cold-start path,
        # not a use of the (meaningless, in this case) override value.
        band_with_bogus_override = ml_model.calibrated_band(
            [], -20.0, 0.0, near_term_half_width=999.0
        )
        band_without_override = ml_model.calibrated_band([], -20.0, 0.0)
        self.assertAlmostEqual(band_with_bogus_override, band_without_override)
