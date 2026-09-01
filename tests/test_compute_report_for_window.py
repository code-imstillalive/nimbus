"""Regression tests for _compute_report_for_window() (issue #316):
the scoring engine extracted from compute_daily_quality_report() so
callers can score arbitrary windows on demand instead of only
"yesterday".

Every existing compute_daily_quality_report() test already covers the
yesterday wrapper path -- those don't need to move. This file focuses
on the extraction's own new surface area:

- allow_partial=False rejects sub-24h windows cleanly
- allow_partial=True scores a partial window without a P2P bonus
- the P2P branch fires only for a real calendar-day-aligned window
- shape errors (end<=start, negative window) return None
- the "yesterday" wrapper still produces the exact same scores as it
  did before the extraction, on the same synthetic scenario every
  other test in test_daily_quality_report.py uses
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


def _fetch(entity_id, start, end):
    """A real, solvable scenario reused across every test below: solar=0
    all window, load=2 kW flat, battery never touched, real cheap-
    overnight/expensive-evening prices. Genuine recoverable arbitrage
    for the oracle, zero for the passive actual trajectory.
    """
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


class TestComputeReportForWindowShape(unittest.TestCase):
    def test_end_equal_to_start_returns_none(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_START, allow_partial=True
            )
        self.assertIsNone(result)

    def test_end_before_start_returns_none(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_END, DAY_START, allow_partial=True
            )
        self.assertIsNone(result)

    def test_missing_solar_sensor_returns_none_without_fetching(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer._compute_report_for_window(
                cfg, DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNone(result)
        fetch.assert_not_called()


class TestAllowPartialGating(unittest.TestCase):
    def test_short_window_with_allow_partial_false_returns_none(self):
        """A six-hour window (< 24 h) must be rejected when the caller
        explicitly asked for full-day scoring semantics -- the same
        contract the yesterday wrapper has always relied on.
        """
        short_end = DAY_START + timedelta(hours=6)
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, short_end, allow_partial=False
            )
        self.assertIsNone(result)

    def test_short_window_with_allow_partial_true_scores_the_window(self):
        """Same six-hour window: with allow_partial=True, the scorer
        must return a real dict scoring exactly that window, not None.
        """
        short_end = DAY_START + timedelta(hours=6)
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, short_end, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertIn("epr", result)
        self.assertIn("j_ref", result)
        self.assertIn("j_ach", result)
        self.assertIn("j_star", result)


class TestP2PCalendarAlignment(unittest.TestCase):
    """The P2P settlement history hook uses the sensor's own history
    dict keyed by ISO date. That lookup is only meaningful when the
    scoring window exactly matches one real calendar day in the local
    timezone. Cross-midnight and partial-day windows must skip the
    branch entirely.
    """

    def test_calendar_day_window_hits_the_p2p_branch(self):
        settlement_state = {
            "state": "ok",
            "attributes": {
                "history": {
                    DAY_START.date().isoformat(): {
                        "export_cost": 4.20,
                        "export_volume": 10.0,
                    }
                }
            },
        }
        cfg = _cfg(solver_p2p_settlement_history_sensor="sensor.p2p_history")
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_fetch
            ),
            patch.object(solver_writer, "ha_get", return_value=settlement_state),
        ):
            result = solver_writer._compute_report_for_window(
                cfg, DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["real_p2p_dollars"], 4.20)
        self.assertEqual(result["real_p2p_volume_kwh"], 10.0)

    def test_cross_midnight_window_skips_the_p2p_branch(self):
        """A window that starts at noon and runs 24 hours crosses one
        midnight. The P2P lookup has no meaningful key to use -- the
        branch must skip, real_p2p_dollars must stay at 0.
        """
        noon_start = DAY_START + timedelta(hours=12)
        noon_end = noon_start + timedelta(hours=24)
        settlement_state = {
            "state": "ok",
            "attributes": {
                "history": {
                    DAY_START.date().isoformat(): {
                        "export_cost": 4.20,
                        "export_volume": 10.0,
                    }
                }
            },
        }
        cfg = _cfg(solver_p2p_settlement_history_sensor="sensor.p2p_history")

        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_fetch
            ),
            patch.object(solver_writer, "ha_get", return_value=settlement_state) as m,
        ):
            result = solver_writer._compute_report_for_window(
                cfg, noon_start, noon_end, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["real_p2p_dollars"], 0.0)
        self.assertEqual(result["real_p2p_volume_kwh"], 0.0)
        # ha_get is also called by _kw_scale_factor() for the three power
        # sensors, so a plain assert_not_called is too strict. What we
        # actually want to check is that the settlement sensor entity_id
        # itself is never looked up.
        for c in m.call_args_list:
            self.assertNotEqual(c.args[0], "sensor.p2p_history")

    def test_partial_day_window_skips_the_p2p_branch(self):
        """A six-hour window that starts at midnight has calendar-date
        alignment on start but not on length. The branch must skip.
        """
        short_end = DAY_START + timedelta(hours=6)
        settlement_state = {
            "state": "ok",
            "attributes": {
                "history": {
                    DAY_START.date().isoformat(): {
                        "export_cost": 4.20,
                        "export_volume": 10.0,
                    }
                }
            },
        }
        cfg = _cfg(solver_p2p_settlement_history_sensor="sensor.p2p_history")

        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_fetch
            ),
            patch.object(solver_writer, "ha_get", return_value=settlement_state) as m,
        ):
            result = solver_writer._compute_report_for_window(
                cfg, DAY_START, short_end, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["real_p2p_dollars"], 0.0)
        for c in m.call_args_list:
            self.assertNotEqual(c.args[0], "sensor.p2p_history")


class TestYesterdayWrapperBackwardCompat(unittest.TestCase):
    """The whole point of a mechanical extraction is that the existing
    yesterday wrapper produces the exact same numbers it did before.
    Score the same real scenario via both entry points and confirm.
    """

    def test_wrapper_output_matches_helper_output_for_yesterday(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_fetch
        ):
            wrapper_result = solver_writer.compute_daily_quality_report(_cfg(), now)
            helper_result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNotNone(wrapper_result)
        self.assertIsNotNone(helper_result)
        # Every key with a scalar value must match exactly. The hourly
        # dicts (j_ref_hourly, j_ach_hourly, j_star_hourly, hourly_regret)
        # are also identical, checked separately for a cleaner assert
        # message when they diverge.
        scalar_keys = [
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
        ]
        for key in scalar_keys:
            self.assertEqual(
                wrapper_result[key],
                helper_result[key],
                f"{key} diverges after extraction",
            )
        for key in ("j_ref_hourly", "j_ach_hourly", "j_star_hourly", "hourly_regret"):
            self.assertEqual(
                wrapper_result[key],
                helper_result[key],
                f"{key} diverges after extraction",
            )


if __name__ == "__main__":
    unittest.main()
