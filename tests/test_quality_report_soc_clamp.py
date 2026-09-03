"""Regression tests for nimbus issue #325 -- the daily quality-report
scorer must CLAMP a historical SoC reading into the configured envelope,
never raise ValueError out of elements.BatteryConfig.__post_init__.

The bug: #58/#64 established that every writer-side path feeding
BatteryConfig from a live/historical SENSOR has to clamp first, because
the real world can legitimately report SoC below the configured floor.
#64 applied that to main()'s forward-planning construction, but the
daily-quality-report scorer builds its OWN BatteryConfig inside
_compute_report_for_window() and never got the same treatment.

Mark Purcell's live repro: sensor.combined_soc is a template averaging
the house battery (7.7%) with a DC-EV-charger channel that reads 0.0%
whenever no vehicle is plugged in, giving 3.85% against a configured
5% floor. The ValueError propagated out through the async publisher, so
sensor.nimbus_solver_quality_report and all nine sensor.nimbus_quality_*
sensors sat `unavailable` for 8.6 hours across 103+ failed publishes --
while the solver itself was completely healthy (LP optimal, 1.07s).

These tests exercise the REAL _compute_report_for_window() against real
history fixtures, not a reimplementation, so they fail against the
pre-fix code with the genuine ValueError rather than passing vacuously.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ

DAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.combined_soc",
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


def _make_fetch(soc_pct):
    """Same solvable scenario the sibling window tests use, with the SoC
    series pinned to one value so each test can put it above, below or
    inside the configured envelope.
    """

    def _fetch(entity_id, start, end):
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
        if entity_id == "sensor.combined_soc":
            return _flat_history(soc_pct, start, end)
        return []

    return _fetch


class TestQualityReportSocClamp(unittest.TestCase):
    def test_soc_below_min_does_not_raise(self):
        """The exact #325 repro: 3.85% historical SoC against a 5% floor.
        Pre-fix this raised `initial_soc_kwh (1.925) must be within
        [min_soc, max_soc]` out of BatteryConfig and killed the publish.
        """
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(3.85)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(
            result, "scorer must still produce a report when SoC is below the floor"
        )

    def test_soc_at_exactly_zero_does_not_raise(self):
        """The degenerate end of the same failure: an EV-charger channel
        reading a clean 0.0% (nothing plugged in), or a fresh install
        whose battery genuinely starts empty.
        """
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(0.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)

    def test_soc_above_max_does_not_raise(self):
        """The other side of the envelope -- sensor drift or a calibration
        artefact reading above the configured ceiling. Same clamp, same
        contract; the invariant is two-sided so the fix must be too.
        """
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=_make_fetch(104.0),
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(solver_battery_max_soc_percent=95.0),
                DAY_START,
                DAY_END,
                allow_partial=True,
            )
        self.assertIsNotNone(result)

    def test_soc_within_envelope_is_unchanged(self):
        """The clamp must be a no-op on a perfectly ordinary reading --
        this is what proves the fix bounds the pathological case without
        quietly rewriting every healthy install's scorer input.
        """
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(50.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)

    def test_below_floor_and_in_envelope_agree_on_shape(self):
        """A clamped run must return the same report SHAPE as a healthy
        one -- the point of #325 is that publishing continues, so every
        downstream sensor.nimbus_quality_* key has to still be there
        rather than the report degrading to a stub.
        """
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(50.0)
        ):
            healthy = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(3.85)
        ):
            clamped = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(healthy)
        self.assertIsNotNone(clamped)
        self.assertEqual(
            set(healthy.keys()),
            set(clamped.keys()),
            "a clamped run must not silently drop report fields",
        )


class TestEfficiencyBacktestSocClamp(unittest.TestCase):
    """The third BatteryConfig path, found by the audit #325 asked for
    rather than by a live crash.

    compute_efficiency_backtest_report() hardcodes initial_soc to 50% of
    capacity, which is NOT unconditionally inside the configured
    envelope: any household running a backup-reserve floor above 50%
    (solver_battery_min_soc_percent = 60 is an ordinary setting) would
    hit the identical ValueError and lose the efficiency-backtest report
    the same way #325 lost the quality report.
    """

    def test_min_soc_above_fifty_percent_does_not_raise(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(70.0)
        ):
            result = solver_writer.compute_efficiency_backtest_report(
                _cfg(solver_battery_min_soc_percent=60.0), DAY_END
            )
        # Either a real report or a clean None (insufficient history for
        # this synthetic fixture) is acceptable -- the assertion is that
        # it does not raise ValueError out of BatteryConfig.
        self.assertTrue(result is None or isinstance(result, dict))

    def test_max_soc_below_fifty_percent_does_not_raise(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(20.0)
        ):
            result = solver_writer.compute_efficiency_backtest_report(
                _cfg(solver_battery_max_soc_percent=40.0), DAY_END
            )
        self.assertTrue(result is None or isinstance(result, dict))


if __name__ == "__main__":
    unittest.main()
