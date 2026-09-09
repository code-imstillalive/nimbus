"""Tests for compute_cost_band() (solver_writer.py), added for nimbus
issue #147: "the load forecast's own uncertainty band is up to 8x the
total cost being optimised, and the LP never sees it". Re-costs the
LP's own COMMITTED dispatch against the load forecast's stated lower/
upper confidence bounds, holding the battery schedule fixed -- read-only
post-hoc analysis, no LP change.
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_writer


def _band(load_lower, load_upper, **overrides):
    n = len(load_lower)
    defaults = {
        "period_hours": np.full(n, 1.0),
        "load_lower_kw": np.array(load_lower),
        "load_upper_kw": np.array(load_upper),
        "solar_kw": np.zeros(n),
        "import_price": np.full(n, 0.20),
        "export_price": np.full(n, 0.05),
        "charge_committed_kw": np.zeros(n),
        "discharge_committed_kw": np.zeros(n),
        "charge_cost": 0.01,
        "discharge_cost_arr": np.full(n, 0.01),
        "final_soc_kwh": 10.0,
        "salvage_value": 0.0,
        "import_limit_kw": 20.0,
        "export_limit_kw": 20.0,
    }
    defaults.update(overrides)
    return solver_writer.compute_cost_band(**defaults)


class TestComputeCostBand(unittest.TestCase):
    def test_wider_load_band_produces_wider_cost_band(self):
        narrow = _band([1.9, 1.9], [2.1, 2.1])
        wide = _band([0.5, 0.5], [3.5, 3.5])
        self.assertIsNotNone(narrow)
        self.assertIsNotNone(wide)
        self.assertGreater(wide["width"], narrow["width"])

    def test_upper_load_band_costs_more_than_lower_with_battery_idle(self):
        # No solar, no battery activity -- every extra kW of load is
        # bought at the flat import price, so a higher load band must
        # cost strictly more. A real, exactly-derivable check, not just
        # "some difference exists".
        band = _band([1.0, 1.0], [3.0, 3.0])
        self.assertIsNotNone(band)
        self.assertAlmostEqual(band["width"], (3.0 - 1.0) * 1.0 * 0.20 * 2, places=6)
        self.assertGreater(band["upper"], band["lower"])

    def test_zero_width_load_band_gives_zero_width_cost_band(self):
        band = _band([2.0, 2.0], [2.0, 2.0])
        self.assertIsNotNone(band)
        self.assertAlmostEqual(band["width"], 0.0, places=6)


class TestPeriodsWithinHours(unittest.TestCase):
    """nimbus issue #630: periods_within_hours() slices
    compute_cost_band()'s own inputs down to "the next 24 real hours"
    for a household-scaled band, distinct from the full 96h one."""

    def test_uniform_grid_picks_the_exact_boundary_count(self):
        # 24 periods of 1h each == exactly 24h -- searchsorted's own
        # side="right" includes the period whose cumulative sum lands
        # exactly ON the boundary, not just strictly under it.
        hours = np.full(30, 1.0)
        self.assertEqual(solver_writer.periods_within_hours(hours, 24.0), 24)

    def test_a_tiered_grid_mixes_fine_and_coarse_periods_correctly(self):
        # Same shape as this project's own real tiered grid: 1-hour
        # periods near "now", then coarser ones -- 20 x 1h (20h) + 2 x
        # 4h (28h total) crosses the 24h mark inside the coarse tier's
        # first period (20h + 4h = 24h exactly).
        hours = np.concatenate([np.full(20, 1.0), np.full(2, 4.0)])
        self.assertEqual(solver_writer.periods_within_hours(hours, 24.0), 21)

    def test_a_single_period_longer_than_the_window_still_returns_one(self):
        # A pathological (or genuinely coarse-only) grid whose very
        # first period alone already exceeds the window -- must not
        # return 0 (an empty slice would crash compute_cost_band()'s
        # own re-costing on a zero-length array).
        hours = np.array([48.0])
        self.assertEqual(solver_writer.periods_within_hours(hours, 24.0), 1)

    def test_window_shorter_than_the_first_period_still_returns_one(self):
        hours = np.array([0.5, 0.5, 0.5])
        self.assertEqual(solver_writer.periods_within_hours(hours, 0.1), 1)

    def test_window_longer_than_the_whole_grid_returns_every_period(self):
        hours = np.full(5, 1.0)
        self.assertEqual(solver_writer.periods_within_hours(hours, 100.0), 5)

    def test_returns_none_on_internal_failure_not_a_crash(self):
        # Mismatched array lengths -- a real internal failure mode this
        # function must degrade gracefully from, since it's a read-only
        # diagnostic riding alongside a real solve that must not break.
        result = solver_writer.compute_cost_band(
            period_hours=np.full(2, 1.0),
            load_lower_kw=np.array([1.0, 1.0, 1.0]),
            load_upper_kw=np.array([2.0, 2.0]),
            solar_kw=np.zeros(2),
            import_price=np.full(2, 0.20),
            export_price=np.full(2, 0.05),
            charge_committed_kw=np.zeros(2),
            discharge_committed_kw=np.zeros(2),
            charge_cost=0.01,
            discharge_cost_arr=np.full(2, 0.01),
            final_soc_kwh=10.0,
            salvage_value=0.0,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        self.assertIsNone(result)
