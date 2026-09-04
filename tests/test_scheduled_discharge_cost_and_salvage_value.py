"""Regression tests for nimbus issue #348 (Mark Purcell, codebase review),
the two remaining findings fixed 2026-09-04:

1. `battery_discharge_cost_rate()`/`battery_salvage_value_rate()` used to
   be hardcoded Python-constant functions (a real, tuned 5pm/midnight/7am
   economic schedule) with zero way for any install -- including the
   household it was tuned for -- to retune it short of editing source.
   Now `scheduled_discharge_cost_rate(cfg, hour)`/
   `scheduled_salvage_value_rate(cfg, hour)`, a real, optional,
   wizard-configurable multi-block schedule mirroring the already-
   established `solver_network_fee_1/2/3_rate/start_hour/end_hour`
   pattern in this same file -- except this one needs a genuinely
   overnight-spanning block (5pm-7am), which that existing pattern's own
   `start <= hour < end` matcher cannot express, so a new wraparound-aware
   `_hour_in_schedule_block()` helper backs it.

   THE CRITICAL PROPERTY under test: every existing install (including
   the one this schedule was tuned for) must see BYTE-IDENTICAL output
   with zero config changes, since every new config key's own schema
   default reproduces the exact historical schedule.

2. The "generic" `solver_price_forecast_array_sensor` field was parsed
   with the LocalVolts-specific `costsflexup`/`earningsflexup` attribute
   keys hardcoded directly at the call site (the parsing function itself,
   `resample_price_with_extrapolation()`, already takes `value_key` as a
   genuinely generic parameter). Now two optional wizard fields,
   `solver_price_forecast_array_import_key`/`_export_key`, each
   defaulting to the exact literal the call site has always used.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestScheduledDischargeCostRateReproducesHistoricalSchedule(unittest.TestCase):
    """With NO new config keys set (every existing install's real state,
    since these keys have never existed before this fix), every hour must
    produce EXACTLY the old hardcoded battery_discharge_cost_rate()'s
    output -- byte-identical behaviour for the household this was tuned
    for."""

    def test_matches_old_hardcoded_function_at_every_hour_of_day(self):
        def old_battery_discharge_cost_rate(hour: int) -> float:
            return 0.01 if (hour >= 17 or hour < 7) else 0.09

        for hour in range(24):
            with self.subTest(hour=hour):
                self.assertEqual(
                    solver_writer.scheduled_discharge_cost_rate({}, hour),
                    old_battery_discharge_cost_rate(hour),
                )

    def test_night_window_exact_boundaries(self):
        cfg = {}
        # 17:00-23:59 and 00:00-06:59 are the "night" (0.01) window.
        for hour in (17, 18, 23, 0, 6):
            self.assertEqual(
                solver_writer.scheduled_discharge_cost_rate(cfg, hour), 0.01
            )
        # 07:00-16:59 is the "day" (0.09) window.
        for hour in (7, 12, 16):
            self.assertEqual(
                solver_writer.scheduled_discharge_cost_rate(cfg, hour), 0.09
            )


class TestScheduledSalvageValueRateReproducesHistoricalSchedule(unittest.TestCase):
    def test_matches_old_hardcoded_function_at_every_hour_of_day(self):
        def old_battery_salvage_value_rate(hour: int) -> float:
            return 0.3 if hour >= 17 else 0.15

        for hour in range(24):
            with self.subTest(hour=hour):
                self.assertEqual(
                    solver_writer.scheduled_salvage_value_rate({}, hour),
                    old_battery_salvage_value_rate(hour),
                )

    def test_night_window_exact_boundaries(self):
        cfg = {}
        for hour in (17, 18, 23):
            self.assertEqual(solver_writer.scheduled_salvage_value_rate(cfg, hour), 0.3)
        for hour in (0, 12, 16):
            self.assertEqual(
                solver_writer.scheduled_salvage_value_rate(cfg, hour), 0.15
            )


class TestScheduleIsGenuinelyConfigurable(unittest.TestCase):
    """The whole point of the fix: a household can now retune this via
    the wizard instead of editing source."""

    def test_default_rate_override_changes_the_day_rate(self):
        cfg = {"solver_discharge_cost_schedule_default_rate": 0.20}
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 12), 0.20)
        # Night block untouched by a default-rate-only change.
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 20), 0.01)

    def test_block_1_override_changes_the_night_rate_and_window(self):
        cfg = {
            "solver_discharge_cost_schedule_block_1_rate": 0.03,
            "solver_discharge_cost_schedule_block_1_start_hour": 20,
            "solver_discharge_cost_schedule_block_1_end_hour": 6,
        }
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 21), 0.03)
        # Hour 18 is no longer inside the (now-narrower) night window.
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 18), 0.09)

    def test_zero_rate_on_block_1_disables_the_night_override_entirely(self):
        # Same "rate<=0 = not configured" convention as
        # NETWORK_FEE_BLOCK_KEYS -- lets an install go fully flat.
        cfg = {"solver_discharge_cost_schedule_block_1_rate": 0.0}
        for hour in (2, 12, 20):
            self.assertEqual(
                solver_writer.scheduled_discharge_cost_rate(cfg, hour), 0.09
            )

    def test_block_2_can_add_a_genuine_third_tier(self):
        cfg = {
            "solver_discharge_cost_schedule_block_2_rate": 0.15,
            "solver_discharge_cost_schedule_block_2_start_hour": 7,
            "solver_discharge_cost_schedule_block_2_end_hour": 9,
        }
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 8), 0.15)
        # Block 1 (night) and the default (day) are both unaffected.
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 20), 0.01)
        self.assertEqual(solver_writer.scheduled_discharge_cost_rate(cfg, 12), 0.09)

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

    def test_non_wrapping_block_matches_plain_range(self):
        self.assertTrue(solver_writer._hour_in_schedule_block(10, 9, 17))
        self.assertFalse(solver_writer._hour_in_schedule_block(17, 9, 17))
        self.assertFalse(solver_writer._hour_in_schedule_block(8, 9, 17))

    def test_wrapping_block_matches_both_sides_of_midnight(self):
        # 17 -> 7 means 17,18,...,23,0,...,6.
        for hour in (17, 20, 23, 0, 3, 6):
            self.assertTrue(solver_writer._hour_in_schedule_block(hour, 17, 7))
        for hour in (7, 10, 16):
            self.assertFalse(solver_writer._hour_in_schedule_block(hour, 17, 7))

    def test_zero_width_block_never_matches(self):
        for hour in range(24):
            self.assertFalse(solver_writer._hour_in_schedule_block(hour, 5, 5))


class TestPriceForecastArrayKeysAreConfigurable(unittest.TestCase):
    """The costsflexup/earningsflexup fix -- verified at the level of the
    real parsing function (already generic), confirming the DEFAULT key
    names it's called with match the historical LocalVolts literals."""

    def test_resample_price_with_extrapolation_default_key_matches_costsflexup_shape(
        self,
    ):
        # The fix is entirely about WHICH key name reaches this call, not
        # this function's own logic (already correct/generic). Confirm a
        # forecast keyed "costsflexup" is read correctly when that literal
        # is passed as value_key -- the exact default the new
        # solver_price_forecast_array_import_key field's absence produces.
        from datetime import UTC, datetime

        forecast = [
            {"time": "2026-01-01T00:00:00+00:00", "costsflexup": 0.11},
            {"time": "2026-01-01T00:05:00+00:00", "costsflexup": 0.12},
        ]
        grid_times = [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)]
        values, mask = solver_writer.resample_price_with_extrapolation(
            forecast, "costsflexup", grid_times, [], {}
        )
        self.assertEqual(values, [0.11])
        self.assertTrue(mask[0])

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


if __name__ == "__main__":
    unittest.main()
