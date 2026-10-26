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


class TestCalibrationHalfWidthPrecomputation(unittest.TestCase):
    """Regression test for nimbus issue #366 finding 2: coordinator.py's
    per-horizon-point loop (~385 points/tick) used to call calibrated_band()
    with no way to avoid recomputing np.percentile(residuals, ...) on every
    single point even though residuals never changes within one cycle.
    calibration_half_width() lets a caller compute that once and pass it
    back in via calibrated_band()'s near_term_half_width override.
    """

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
