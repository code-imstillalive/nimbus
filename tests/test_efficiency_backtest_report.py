"""Tests for compute_efficiency_backtest_report()/publish_efficiency_
backtest_report() (solver_writer.py) -- the retrospective backtesting
engine's first real check (2026-08-25, direct household ask for a
genuine "outstanding, unique" idea). See solver/backtest.py's own
module docstring for the full "what this can and cannot test"
reasoning, and tests/test_solver_backtest.py for the underlying pure
math/LP tests this file does NOT re-test -- these tests exercise the
solver_writer.py WIRING (cfg gating, real history reconstruction,
publish idempotency) the same way test_daily_quality_report.py already
does for its sibling function.
"""

import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


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


def _flat_history(value, day_start, day_end, step_minutes=15):
    out = []
    t = day_start
    while t < day_end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _price_history(day_start, cheap=0.05, expensive=0.35, expensive_hour=17):
    out = []
    t = day_start
    while t < day_start + timedelta(days=1):
        out.append((t, expensive if t.hour >= expensive_hour else cheap))
        t += timedelta(minutes=15)
    return out


NOW = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
YESTERDAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
YESTERDAY_END = YESTERDAY_START + timedelta(days=1)


class TestComputeEfficiencyBacktestReportGating(unittest.TestCase):
    def test_missing_solar_sensor_returns_none_without_fetching_anything(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.compute_efficiency_backtest_report(cfg, NOW)
        self.assertIsNone(result)
        fetch.assert_not_called()

    def test_missing_battery_sensor_returns_none(self):
        cfg = _cfg(solver_battery_power_sensor=None)
        self.assertIsNone(solver_writer.compute_efficiency_backtest_report(cfg, NOW))

    def test_missing_load_sensor_returns_none(self):
        cfg = _cfg(solver_whole_house_cross_check_sensor=None)
        self.assertIsNone(solver_writer.compute_efficiency_backtest_report(cfg, NOW))

    def test_empty_solar_history_returns_none(self):
        cfg = _cfg()

        def fake_fetch(entity_id, start, end):
            if entity_id == "sensor.real_solar":
                return []
            return _flat_history(1.0, YESTERDAY_START, YESTERDAY_END)

        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=fake_fetch
        ):
            self.assertIsNone(
                solver_writer.compute_efficiency_backtest_report(cfg, NOW)
            )


class TestComputeEfficiencyBacktestReportRealSweep(unittest.TestCase):
    """A real day with a genuine cheap-overnight/expensive-evening
    arbitrage opportunity and real solar -- the exact shape needed for
    a lower efficiency candidate to genuinely score worse than a higher
    one (a flat/degenerate day would make every candidate identical,
    proving nothing)."""

    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            # Midday solar block -- real, not flat.
            out = []
            t = YESTERDAY_START
            while t < YESTERDAY_END:
                out.append((t, 6.0 if 9 <= t.hour < 15 else 0.0))
                t += timedelta(minutes=15)
            return out
        if entity_id == "sensor.real_load":
            return _flat_history(1.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def test_real_sweep_produces_a_full_ranked_report(self):
        cfg = _cfg()
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_efficiency_backtest_report(cfg, NOW)
        self.assertIsNotNone(report)
        for key in (
            "candidates",
            "configured_efficiency_percent",
            "best_candidate",
            "best_candidate_cost",
            "worst_candidate",
            "worst_candidate_cost",
            "spread_dollars",
        ):
            self.assertIn(key, report)
        self.assertEqual(len(report["candidates"]), 4)
        self.assertEqual(report["configured_efficiency_percent"], 95.0)
        # best <= worst by construction (min/max of the same set).
        self.assertLessEqual(
            report["best_candidate_cost"], report["worst_candidate_cost"]
        )
        self.assertGreaterEqual(report["spread_dollars"], 0.0)
        # A real day with a genuine arbitrage opportunity should show
        # SOME real sensitivity to efficiency, not an exact-zero spread.
        self.assertGreater(report["spread_dollars"], 0.0)


class TestPublishEfficiencyBacktestReportIdempotency(unittest.TestCase):
    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(3.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(1.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def test_already_scored_day_skips_recompute_but_still_repushes(self):
        """Real fix (2026-08-30, issues #289/#292): the fast path must
        still re-push the SAME already-read state/attributes -- see
        test_daily_quality_report.py's own sibling test for the full
        "why" (skipping the publish entirely let this entity's
        freshness stamp go stale and get marked unavailable, forever)."""
        cfg = _cfg()
        existing_state = {
            "state": "1.23",
            "attributes": {"latest_date": (NOW - timedelta(days=1)).date().isoformat()},
        }
        with (
            patch.object(solver_writer, "ha_get", return_value=existing_state),
            patch.object(solver_writer, "fetch_entity_history_range") as fetch,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_efficiency_backtest_report(cfg, NOW)
        fetch.assert_not_called()
        post.assert_called_once_with(
            solver_writer.BACKTEST_ENTITY_ID,
            existing_state["state"],
            existing_state["attributes"],
        )

    def test_not_yet_scored_day_computes_and_publishes(self):
        cfg = _cfg()
        # A plain URLError, not a constructed HTTPError(fp=None) --
        # HTTPError's own real __init__ wraps `fp` in a way that (on some
        # Python versions, confirmed on 3.14 in CI) leaves an unclosed
        # implicit temp file if fp is None, firing an unrelated
        # ResourceWarning later during a completely different test.
        # publish_efficiency_backtest_report()'s own except clause
        # already catches URLError (HTTPError's own superclass) too, so
        # this exercises the identical code path without that trap.
        not_found = urllib.error.URLError("Not Found")
        with (
            patch.object(solver_writer, "ha_get", side_effect=not_found),
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_efficiency_backtest_report(cfg, NOW)
        post.assert_called_once()
        entity_id, _state, attrs = post.call_args[0]
        self.assertEqual(entity_id, solver_writer.BACKTEST_ENTITY_ID)
        self.assertIn("candidates", attrs)
        self.assertEqual(
            attrs["latest_date"], (NOW - timedelta(days=1)).date().isoformat()
        )
