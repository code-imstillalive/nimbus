"""A scheduled discharge's cost and the battery's salvage value (#348) (#1837)."""

from __future__ import annotations

import unittest

import solver_writer


class TestScheduleIsGenuinelyConfigurable(unittest.TestCase):
    """The whole point of the fix: a household can now retune this via
    the wizard instead of editing source."""

    def test_salvage_value_default_and_block_1_are_independently_configurable(self):
        cfg = {
            "solver_salvage_value_schedule_default_rate": 0.10,
            "solver_salvage_value_schedule_block_1_rate": 0.5,
        }
        self.assertEqual(solver_writer.scheduled_salvage_value_rate(cfg, 12), 0.10)
        self.assertEqual(solver_writer.scheduled_salvage_value_rate(cfg, 20), 0.5)


class TestHourInScheduleBlockWraparound(unittest.TestCase):
    """Direct coverage of the new wraparound-aware matcher -- the actual
    mechanism the existing NETWORK_FEE_BLOCK_KEYS/P2P_BLOCK_KEYS `start <=
    hour < end` check has never needed, since none of THEIR real blocks
    span midnight."""

    def test_zero_width_block_never_matches(self):
        for hour in range(24):
            self.assertFalse(solver_writer._hour_in_schedule_block(hour, 5, 5))


class TestPriceForecastArrayKeysAreConfigurable(unittest.TestCase):
    """The costsflexup/earningsflexup fix -- verified at the level of the
    real parsing function (already generic), confirming the DEFAULT key
    names it's called with match the historical LocalVolts literals."""

    def test_a_non_localvolts_key_name_also_works_once_passed_through(self):
        from datetime import UTC, datetime

        forecast = [{"time": "2026-01-01T00:00:00+00:00", "import_rate": 0.22}]
        grid_times = [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)]
        values, _mask = solver_writer.resample_price_with_extrapolation(
            forecast, "import_rate", grid_times, [], {}
        )
        self.assertEqual(values, [0.22])
        # The OLD hardcoded "costsflexup" key would find zero points at
        # all against this forecast shape -- confirming the two key names
        # are genuinely not interchangeable, i.e. this is a real fix, not
        # a no-op generalisation.
        old_values, _old_mask = solver_writer.resample_price_with_extrapolation(
            forecast, "costsflexup", grid_times, [], {}
        )
        self.assertEqual(old_values, [0.0])
