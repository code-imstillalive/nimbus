"""Regression tests for compute_daily_flex_report()/_compute_flex_report_
for_window() (nimbus issue #496, Signals 7/7 of #489) -- the daily-report
half Mark Purcell explicitly authorized 2026-09-09 ("go ahead and build
the non-schema-dependent half now... compute_daily_flex_report()'s own
offered-vs-realised/envelope-curtailment arithmetic"), shipped separately
from the sensor half (switch.nimbus_solver_flex_signals_enabled,
sensor.nimbus_flex_signals -- v0.94.282/v0.94.283).

Same test-doubling convention as test_compute_report_for_window.py:
patches fetch_entity_history_range/fetch_entity_attribute_history_range
directly (the real functions, not a reimplementation), synthetic but
physically real scenarios.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ

DAY_START = datetime(2026, 9, 13, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_grid_max_export_kw": 5.0,
    }
    cfg.update(overrides)
    return cfg


def _flat_history(value, start, end, step_minutes=30):
    out = []
    t = start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _make_fetch(solar=0.0, load=2.0, battery=0.0, price=0.20, offered_up=None):
    """offered_up=None means sensor.nimbus_flex_signals has NO history
    this day (the opt-in switch was off) -- the real, honest case
    compute_daily_flex_report() must degrade gracefully for."""

    def _fetch(entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(solar, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(load, start, end)
        if entity_id == "sensor.real_battery":
            return _flat_history(battery, start, end)
        if entity_id == "sensor.import_price":
            return _flat_history(price, start, end)
        if entity_id == solver_writer.FLEX_SIGNALS_ENTITY_ID:
            if offered_up is None:
                return []
            return _flat_history(offered_up, start, end)
        return []

    return _fetch


def _make_attr_fetch(offered_down=None):
    def _fetch(entity_id, attribute, start, end):
        if (
            entity_id == solver_writer.FLEX_SIGNALS_ENTITY_ID
            and attribute == "flex_available_down_kw"
            and offered_down is not None
        ):
            return _flat_history(offered_down, start, end)
        return []

    return _fetch


class TestComputeDailyFlexReportShape(unittest.TestCase):
    def test_missing_sensor_config_returns_none(self):
        result = solver_writer.compute_daily_flex_report(
            _cfg(solver_solar_power_sensor=None), DAY_END
        )
        self.assertIsNone(result)

    def test_window_shorter_than_24h_returns_none(self):
        result = solver_writer._compute_flex_report_for_window(
            _cfg(), DAY_START, DAY_START + timedelta(hours=6)
        )
        self.assertIsNone(result)

    def test_end_not_after_start_returns_none(self):
        result = solver_writer._compute_flex_report_for_window(
            _cfg(), DAY_START, DAY_START
        )
        self.assertIsNone(result)

    def test_missing_base_history_returns_none(self):
        with patch.object(solver_writer, "fetch_entity_history_range", return_value=[]):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        self.assertIsNone(result)


class TestComputeDailyFlexReportOfferedVsRealised(unittest.TestCase):
    def test_no_flex_signals_history_leaves_offered_none_not_zero(self):
        """The real, honest degrade: switch was off this day, so
        sensor.nimbus_flex_signals has no recorder history at all --
        offered_up_kwh/offered_down_kwh must be None (unknown), never a
        fabricated 0.0, matching this project's own established
        'represent honestly' convention."""
        fetch = _make_fetch(offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        self.assertIsNotNone(result)
        self.assertIsNone(result["offered_up_kwh"])
        self.assertIsNone(result["offered_down_kwh"])

    def test_flex_signals_history_present_populates_offered(self):
        fetch = _make_fetch(offered_up=1.5)
        attr_fetch = _make_attr_fetch(offered_down=2.5)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        self.assertIsNotNone(result)
        # 1.5 kW flat for 24h = 36 kWh
        self.assertAlmostEqual(result["offered_up_kwh"], 36.0, places=1)
        self.assertAlmostEqual(result["offered_down_kwh"], 60.0, places=1)

    def test_realised_up_down_derived_from_real_battery_history(self):
        """battery=-3.0 kW flat (positive=discharge convention, the
        default) means the battery is charging at 3 kW the whole day --
        realised_up_kwh (absorbing more load = charging) should reflect
        that, realised_down_kwh (discharging) should be ~0."""
        fetch = _make_fetch(battery=-3.0, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        self.assertAlmostEqual(result["realised_up_kwh"], 72.0, places=1)  # 3kW*24h
        self.assertAlmostEqual(result["realised_down_kwh"], 0.0, places=1)

    def test_realised_sign_convention_respects_positive_is_charge_flag(self):
        """Same real bug class as issue #299 (compute_daily_quality_
        report()'s own battery_sign handling) -- a SigEnergy-style
        install with solver_battery_power_positive_is_charge=True must
        flip which side (up/down) a positive reading represents."""
        fetch = _make_fetch(battery=3.0, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(solver_battery_power_positive_is_charge=True), DAY_START, DAY_END
            )
        self.assertAlmostEqual(result["realised_up_kwh"], 72.0, places=1)
        self.assertAlmostEqual(result["realised_down_kwh"], 0.0, places=1)


class TestComputeDailyFlexReportEnvelopeCurtailment(unittest.TestCase):
    def test_curtailment_zero_when_solar_never_exceeds_load_plus_limit(self):
        fetch = _make_fetch(solar=1.0, load=2.0, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(solver_grid_max_export_kw=5.0), DAY_START, DAY_END
            )
        self.assertAlmostEqual(result["envelope_curtailment_kwh"], 0.0, places=1)

    def test_curtailment_positive_when_solar_exceeds_load_plus_limit(self):
        """solar=10kW, load=2kW, export_limit=5kW -> 10-2-5=3kW curtailed
        every period, real configured limit used, not a hardcoded 5kW
        default (this issue's own explicit warning)."""
        fetch = _make_fetch(solar=10.0, load=2.0, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(solver_grid_max_export_kw=5.0), DAY_START, DAY_END
            )
        self.assertAlmostEqual(result["envelope_curtailment_kwh"], 72.0, places=1)

    def test_curtailment_uses_the_real_configured_export_limit_not_a_hardcoded_default(
        self,
    ):
        """Same scenario, different configured limit -- confirms the
        real cfg value drives the result, not a fixed constant."""
        fetch = _make_fetch(solar=10.0, load=2.0, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result_low_limit = solver_writer._compute_flex_report_for_window(
                _cfg(solver_grid_max_export_kw=1.0), DAY_START, DAY_END
            )
            result_high_limit = solver_writer._compute_flex_report_for_window(
                _cfg(solver_grid_max_export_kw=20.0), DAY_START, DAY_END
            )
        self.assertGreater(
            result_low_limit["envelope_curtailment_kwh"],
            result_high_limit["envelope_curtailment_kwh"],
        )
        self.assertAlmostEqual(
            result_high_limit["envelope_curtailment_kwh"], 0.0, places=1
        )


class TestComputeDailyFlexReportPriceResponseCurve(unittest.TestCase):
    def test_single_price_produces_one_band_with_all_samples(self):
        fetch = _make_fetch(price=0.22, offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        curve = result["price_response_curve"]
        self.assertEqual(len(curve), 1)
        self.assertEqual(curve[0]["price_band_low"], 0.20)
        self.assertEqual(curve[0]["price_band_high"], 0.25)
        self.assertEqual(curve[0]["n_samples"], 48)  # 24h at 30-min periods

    def test_net_import_kw_reflects_real_energy_balance(self):
        """load=5, solar=2, battery=0 -> net_import should be ~3kW at
        whatever single price band the flat price falls into."""
        fetch = _make_fetch(
            solar=2.0, load=5.0, battery=0.0, price=0.30, offered_up=None
        )
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer._compute_flex_report_for_window(
                _cfg(), DAY_START, DAY_END
            )
        curve = result["price_response_curve"]
        self.assertEqual(len(curve), 1)
        self.assertAlmostEqual(curve[0]["mean_net_import_kw"], 3.0, places=1)


class TestComputeDailyFlexReportYesterdayWrapper(unittest.TestCase):
    def test_yesterday_wrapper_produces_a_real_date_and_matches_window_version(self):
        now = DAY_END + timedelta(hours=2)
        fetch = _make_fetch(offered_up=None)
        attr_fetch = _make_attr_fetch(offered_down=None)
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=fetch
            ),
            patch.object(
                solver_writer,
                "fetch_entity_attribute_history_range",
                side_effect=attr_fetch,
            ),
        ):
            result = solver_writer.compute_daily_flex_report(_cfg(), now)
        self.assertIsNotNone(result)
        self.assertEqual(result["latest_date"], DAY_START.date().isoformat())


if __name__ == "__main__":
    unittest.main()
