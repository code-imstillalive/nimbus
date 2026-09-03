"""Real, confirmed-live bug found by Mark Purcell (issue #299, 2026-08-31):
compute_daily_quality_report() always assumed the configured battery power
sensor follows this project's own established convention (positive =
discharge, matching the reference household's real
sensor.logger_battery_power) with no way to say otherwise. A SigEnergy
plant's own sensor.sigen_plant_battery_power uses the OPPOSITE convention
(positive = charge). Confirmed live on Mark's install: while genuinely
discharging 2kW, the sensor read -0.008kW -- every charge event was
silently booked as a discharge and vice versa, producing EPR = -137.47%
(structurally impossible: EPR can never go negative when scored correctly,
since a perfect-foresight oracle can never be beaten).

Fixed with CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE (default False,
Mark's own "Option A" -- an explicit config-flow flag, not silent
auto-detection). These tests lock in both halves of the guarantee: the
default reproduces the ORIGINAL, undocumented assumption byte-for-byte
(zero behaviour change for every existing install, including this
project's own reference household), and flipping the flag genuinely
inverts the interpretation for a household whose hardware reports the
opposite way.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
YESTERDAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
YESTERDAY_END = YESTERDAY_START + timedelta(days=1)


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


class TestBatteryPowerSignConvention(unittest.TestCase):
    """A real, genuinely charging battery (-5kW under this project's own
    convention) is unambiguous under both readings: a SigEnergy-style
    sensor would report it as +5kW instead. Spy on the real
    elements.BatteryConfig call site's SIBLING inputs by capturing
    evaluate_realized_cost()'s own charge/discharge arrays instead --
    the cleanest, most direct way to observe what
    compute_daily_quality_report() actually decided the household did.
    """

    def _fetch_side_effect(self, entity_id, start, end, battery_reading):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(3.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            return _flat_history(battery_reading, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def _run_and_capture_actual_net(self, cfg, battery_reading):
        """Runs compute_daily_quality_report() and returns the real
        actual_charge_kw/actual_discharge_kw arrays it built, by spying on
        the single real call to compute_quality_report() (solver/
        quality_report.py) -- these two are direct, named keyword
        arguments to that call, the cleanest possible interception point
        for what this function actually decided the household's real
        battery did."""
        real_compute = solver_writer.compute_quality_report
        captured = {}

        def spy(**kwargs):
            captured["charge"] = kwargs["actual_charge_kw"]
            captured["discharge"] = kwargs["actual_discharge_kw"]
            return real_compute(**kwargs)

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=lambda e, s, en: self._fetch_side_effect(
                    e, s, en, battery_reading
                ),
            ),
            patch.object(solver_writer, "compute_quality_report", side_effect=spy),
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        return captured["charge"], captured["discharge"]

    def test_default_reproduces_original_assumption_byte_for_byte(self):
        """A +5kW reading, flag unset (this project's own reference
        household's real convention): must be read as DISCHARGE, exactly
        the original, undocumented behaviour -- zero change for every
        existing install."""
        cfg = _cfg()
        charge, discharge = self._run_and_capture_actual_net(cfg, 5.0)
        self.assertTrue((discharge == 5.0).all())
        self.assertTrue((charge == 0.0).all())

    def test_flag_false_explicit_same_as_default(self):
        cfg = _cfg(solver_battery_power_positive_is_charge=False)
        charge, discharge = self._run_and_capture_actual_net(cfg, 5.0)
        self.assertTrue((discharge == 5.0).all())
        self.assertTrue((charge == 0.0).all())

    def test_flag_true_inverts_a_sigenergy_style_reading(self):
        """The exact real-world case (issue #299): a +5kW reading on a
        SigEnergy-convention sensor means CHARGING, not discharging."""
        cfg = _cfg(solver_battery_power_positive_is_charge=True)
        charge, discharge = self._run_and_capture_actual_net(cfg, 5.0)
        self.assertTrue((charge == 5.0).all())
        self.assertTrue((discharge == 0.0).all())

    def test_flag_true_a_negative_sigenergy_reading_is_discharge(self):
        """The mirror case: -5kW on a SigEnergy-convention sensor means
        DISCHARGING."""
        cfg = _cfg(solver_battery_power_positive_is_charge=True)
        charge, discharge = self._run_and_capture_actual_net(cfg, -5.0)
        self.assertTrue((discharge == 5.0).all())
        self.assertTrue((charge == 0.0).all())


if __name__ == "__main__":
    unittest.main()
