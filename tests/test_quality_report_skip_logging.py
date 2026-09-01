"""Regression tests for nimbus issues #313/#314 (Mark Purcell): every
silent-skip path in publish_daily_quality_report()/
_compute_report_for_window() used to return None or bare `return` with
zero logging, making a real 14-hour scoring freeze indistinguishable
from "no history yet" from "sensor mapping wrong" from "solver LP
infeasible" -- all four external symptoms were identical (sensor stays
at its last known state, no log line, no attribute change).

Each test below forces one specific skip path and asserts the exact
log line Mark's own issues specified fires at the level he asked for
(DEBUG for routine/expected skips, INFO for a real history gap, WARNING
for a genuinely infeasible oracle solve).
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.BRISBANE_TZ
LOGGER_NAME = solver_writer._LOGGER.name


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
    }
    cfg.update(overrides)
    return cfg


def _flat_history(value, start, end, step_minutes=15):
    out = []
    t = start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _price_history(start, end, cheap=0.05, expensive=0.35, expensive_hour=17):
    out = []
    t = start
    while t < end:
        out.append((t, expensive if t.hour >= expensive_hour else cheap))
        t += timedelta(minutes=15)
    return out


DAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)


def _full_fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _flat_history(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat_history(2.0, start, end)
    if entity_id == "sensor.real_battery":
        return _flat_history(0.0, start, end)
    if entity_id == "sensor.import_price":
        return _price_history(start, end)
    if entity_id == "sensor.export_price":
        return _price_history(start, end, cheap=0.02, expensive=0.10)
    return []


class TestComputeReportForWindowSkipLogging(unittest.TestCase):
    def test_missing_sensor_config_logs_debug(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with self.assertLogs(LOGGER_NAME, level="DEBUG") as cm:
            result = solver_writer._compute_report_for_window(
                cfg, DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNone(result)
        self.assertTrue(
            any("Missing sensor config" in line for line in cm.output),
            cm.output,
        )

    def test_short_window_rejected_logs_debug(self):
        short_end = DAY_START + timedelta(hours=6)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_full_fetch
            ),
            self.assertLogs(LOGGER_NAME, level="DEBUG") as cm,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, short_end, allow_partial=False
            )
        self.assertIsNone(result)
        self.assertTrue(
            any("shorter than the 24" in line for line in cm.output), cm.output
        )

    def test_missing_history_logs_info_with_row_counts(self):
        """issue #314's own root-cause hypothesis #1 -- history genuinely
        empty for the window. Must log at INFO (not DEBUG), naming the
        exact per-sensor row counts, per Mark's own proposed fix."""
        with (
            patch.object(solver_writer, "fetch_entity_history_range", return_value=[]),
            self.assertLogs(LOGGER_NAME, level="INFO") as cm,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNone(result)
        self.assertTrue(
            any(
                "Real history missing" in line and "solar=0" in line
                for line in cm.output
            ),
            cm.output,
        )

    def test_oracle_infeasible_logs_warning_with_soc_values(self):
        """issue #314's own root-cause hypothesis #3 -- oracle LP
        infeasible. Must log at WARNING, naming initial_soc/min_soc/
        max_soc, per Mark's own proposed fix and his own 2026-08-30
        hand-diagnosis of this exact failure shape."""
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_full_fetch
            ),
            patch.object(
                solver_writer,
                "compute_quality_report",
                side_effect=RuntimeError("infeasible"),
            ),
            self.assertLogs(LOGGER_NAME, level="WARNING") as cm,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNone(result)
        self.assertTrue(
            any(
                "Oracle LP infeasible" in line
                and "initial_soc=" in line
                and "min_soc=" in line
                and "max_soc=" in line
                for line in cm.output
            ),
            cm.output,
        )


class TestPublishDailyQualityReportSkipLogging(unittest.TestCase):
    NOW = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)

    def test_fast_path_hit_logs_debug(self):
        existing = {
            "state": 72.74,
            "attributes": {"latest_date": "2026-08-24"},
        }
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(solver_writer, "ha_post_state") as post,
            self.assertLogs(LOGGER_NAME, level="DEBUG") as cm,
        ):
            solver_writer.publish_daily_quality_report(_cfg(), self.NOW)
        post.assert_called_once()
        self.assertTrue(any("fast-path hit" in line for line in cm.output), cm.output)

    def test_compute_none_logs_debug_and_does_not_publish(self):
        with (
            patch.object(
                solver_writer,
                "ha_get",
                side_effect=solver_writer.urllib.error.URLError("unreachable"),
            ),
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=None
            ),
            patch.object(solver_writer, "ha_post_state") as post,
            self.assertLogs(LOGGER_NAME, level="DEBUG") as cm,
        ):
            solver_writer.publish_daily_quality_report(_cfg(), self.NOW)
        post.assert_not_called()
        self.assertTrue(any("no report for" in line for line in cm.output), cm.output)


if __name__ == "__main__":
    unittest.main()
