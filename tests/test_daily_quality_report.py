"""Tests for the built-in EPR/regret/tracking quality score (2026-08-25,
direct ask: "it should be a part of the suite to monitor epr and trend
and regret... nimbus should have it built in") -- compute_daily_quality_
report()/publish_daily_quality_report() (solver_writer.py), a from-
scratch generalization of the household-specific reference script
(docs/real-world-integration/files/nimbus_solver_quality_writer.py)
that uses only genuinely portable inputs (real recorder history via the
two new CONF_SOLVER_SOLAR_POWER_SENSOR/CONF_SOLVER_BATTERY_POWER_SENSOR
fields, plus the existing CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR for
load) instead of one household's own LocalVolts/Sungrow/Modbus stack.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.BRISBANE_TZ


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


class TestComputeDailyQualityReportGating(unittest.TestCase):
    def test_missing_solar_sensor_returns_none_without_fetching_anything(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNone(result)
        fetch.assert_not_called()

    def test_missing_battery_sensor_returns_none(self):
        cfg = _cfg(solver_battery_power_sensor=None)
        self.assertIsNone(solver_writer.compute_daily_quality_report(cfg, NOW))

    def test_missing_load_sensor_returns_none(self):
        cfg = _cfg(solver_whole_house_cross_check_sensor=None)
        self.assertIsNone(solver_writer.compute_daily_quality_report(cfg, NOW))

    def test_empty_history_for_any_signal_returns_none(self):
        cfg = _cfg()

        def fake_fetch(entity_id, start, end):
            if entity_id == "sensor.real_solar":
                return []  # genuinely unavailable for yesterday
            return _flat_history(1.0, YESTERDAY_START, YESTERDAY_END)

        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=fake_fetch
        ):
            self.assertIsNone(solver_writer.compute_daily_quality_report(cfg, NOW))


class TestComputeDailyQualityReportRealScore(unittest.TestCase):
    """A real, solvable scenario: solar=0 all day, a constant 2kW load,
    battery never touched (actual_net_kw=0 all day, SoC history flat) --
    real cheap-overnight/expensive-evening prices create genuine
    recoverable arbitrage for a perfect-foresight oracle, while the
    passive actual trajectory captures none of it.
    """

    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def test_real_recoverable_regret_is_captured(self):
        cfg = _cfg()
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        for key in (
            "epr",
            "theoretical_maximum_yield",
            "value_captured",
            "uplift_available",
            "j_ref",
            "j_ach",
            "j_star",
            "regret_dollars",
            "tracking_fidelity",
            "tracking_cost",
            "real_p2p_dollars",
            "real_p2p_volume_kwh",
        ):
            self.assertIn(key, report)

        # No generic commanded-dispatch signal exists (see the function's
        # own docstring) -- commanded is set equal to actual by
        # construction, so tracking must be exactly perfect every time.
        self.assertEqual(report["tracking_fidelity"], 1.0)
        self.assertEqual(report["tracking_cost"], 0.0)

        # The battery genuinely never moved (flat 0 net power, flat SoC
        # history) -- j_ach's own residual evaluation is then IDENTICAL
        # to j_ref's (same zero charge/discharge, same start==final SoC),
        # a real, exactly-verifiable structural property, not just "close".
        self.assertAlmostEqual(report["j_ach"], report["j_ref"], places=6)

        # A real LP oracle can never do WORSE than the passive baseline
        # it's being compared against -- genuine recoverable regret from
        # the cheap/expensive price spread this scenario deliberately
        # engineers in.
        self.assertLessEqual(report["j_star"], report["j_ach"] + 1e-9)
        self.assertGreater(report["regret_dollars"], 0.0)
        self.assertLess(report["epr"], 1.0)

        # No settlement hook configured -- real_p2p fields stay exactly
        # zero, never fabricated.
        self.assertEqual(report["real_p2p_dollars"], 0.0)
        self.assertEqual(report["real_p2p_volume_kwh"], 0.0)

    def test_oracle_infeasible_solve_degrades_to_none_not_a_crash(self):
        cfg = _cfg()
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(
                solver_writer,
                "compute_quality_report",
                side_effect=RuntimeError("Oracle solve failed"),
            ),
        ):
            self.assertIsNone(solver_writer.compute_daily_quality_report(cfg, NOW))


class TestSettlementHook(unittest.TestCase):
    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def test_settlement_sensor_populates_real_p2p_fields(self):
        cfg = _cfg(solver_p2p_settlement_history_sensor="sensor.real_settlement")
        settlement_state = {
            "attributes": {
                "history": {"2026-08-24": {"export_cost": 12.5, "export_volume": 50.0}}
            }
        }
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(solver_writer, "ha_get", return_value=settlement_state),
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        self.assertEqual(report["real_p2p_dollars"], 12.5)
        self.assertEqual(report["real_p2p_volume_kwh"], 50.0)

    def test_settlement_sensor_missing_yesterdays_entry_falls_back_to_zero(self):
        cfg = _cfg(solver_p2p_settlement_history_sensor="sensor.real_settlement")
        settlement_state = {"attributes": {"history": {"2026-08-01": {}}}}
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(solver_writer, "ha_get", return_value=settlement_state),
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertEqual(report["real_p2p_dollars"], 0.0)
        self.assertEqual(report["real_p2p_volume_kwh"], 0.0)


class TestResampleHistoryNearest(unittest.TestCase):
    def test_nearest_at_or_before_real_points(self):
        anchor = YESTERDAY_START
        pts = [
            (anchor, 1.0),
            (anchor + timedelta(hours=1), 2.0),
            (anchor + timedelta(hours=2), 3.0),
        ]
        grid = [anchor + timedelta(minutes=90)]
        self.assertEqual(solver_writer.resample_history_nearest(pts, grid), [2.0])

    def test_empty_history_returns_default_for_every_grid_point(self):
        grid = [YESTERDAY_START, YESTERDAY_START + timedelta(hours=1)]
        self.assertEqual(
            solver_writer.resample_history_nearest([], grid, default=0.42),
            [0.42, 0.42],
        )


class TestPublishDailyQualityReport(unittest.TestCase):
    def test_already_scored_yesterday_skips_recompute_and_repush(self):
        cfg = _cfg()
        existing = {"attributes": {"latest_date": "2026-08-24"}}
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(solver_writer, "compute_daily_quality_report") as compute,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_daily_quality_report(cfg, NOW)
        compute.assert_not_called()
        post.assert_not_called()

    def test_not_yet_scored_computes_and_pushes(self):
        cfg = _cfg()
        day_entry = {
            "epr": 0.5,
            "theoretical_maximum_yield": 1.0,
            "value_captured": 0.5,
            "uplift_available": 0.5,
            "j_ref": 10.0,
            "j_ach": 8.0,
            "j_star": 6.0,
            "regret_dollars": 2.0,
            "tracking_fidelity": 1.0,
            "tracking_cost": 0.0,
            "real_p2p_dollars": 0.0,
            "real_p2p_volume_kwh": 0.0,
        }
        with (
            patch.object(
                solver_writer,
                "ha_get",
                side_effect=solver_writer.urllib.error.URLError("not found"),
            ),
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=day_entry
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_daily_quality_report(cfg, NOW)
        post.assert_called_once()
        entity_id, state, attrs = post.call_args[0]
        self.assertEqual(entity_id, solver_writer.QUALITY_ENTITY_ID)
        self.assertEqual(state, 0.5)
        self.assertEqual(attrs["latest_date"], "2026-08-24")
        self.assertEqual(attrs["epr"], 0.5)

    def test_compute_returning_none_never_pushes(self):
        cfg = _cfg()
        with (
            patch.object(
                solver_writer,
                "ha_get",
                side_effect=solver_writer.urllib.error.URLError("not found"),
            ),
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=None
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_daily_quality_report(cfg, NOW)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
