"""nimbus issue #1149 -- `energy_decomposition`, the "what did the
controller do differently" companion to `hourly_regret`.

`hourly_regret` answers "which hour cost money". It cannot answer "what
did the controller do differently", and when the difference is ONE
decision spread over several hours it actively obscures it:
`hourly_regret_breakdown()`'s own docstring already warns the buckets
can be large in both directions and cancel.

Measured on a real install (2026-09-20, reference household, 19 Sep
scored): hourly regret ran -$6.17 at 11:00 against +$11.46 across
12:00-15:00 -- a gross positive 3.1x the $3.65 net, with the largest-
magnitude hour being a NEGATIVE one. Ranking hours points at 14:00. The
decomposition says it in one line: achieved charged 108.1 kWh against
the oracle's 83.1 and imported 60.0 against 39.8, while evening
discharge differed by only ~3.5 kWh. One over-charge, not five findings.

Only the achieved side was previously derivable, and only partly --
`fleet_achieved_energy_in_kwh`/`_out_kwh` exist, but no oracle
counterpart and no grid figure for either, so that delta could not be
computed from the sensor at all.

Uses the same real `_compute_report_for_window()` fixture pattern as
test_quality_report_achieved_energy_and_reliability.py -- exercises the
real function, not a reimplementation.
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

ROWS = ("reference", "achieved", "oracle", "achieved_minus_oracle")
FIELDS = ("charge_kwh", "discharge_kwh", "grid_import_kwh", "grid_export_kwh")

# The fixture holds load flat at 2.0 kW and solar at 0.0 all day, so
# every trajectory's grid flow is exactly `2.0 + battery_net_kw` -- which
# makes the expected import/export arithmetic below a real hand
# calculation rather than whatever the code happens to produce.
FIXTURE_LOAD_KW = 2.0
HOURS_IN_DAY = 24.0


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


def _make_fetch(battery_kw, soc_pct=50.0):
    """Positive = discharge, this project's own established convention."""

    def _fetch(entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(FIXTURE_LOAD_KW, start, end)
        if entity_id == "sensor.real_battery":
            return _flat_history(battery_kw, start, end)
        if entity_id == "sensor.import_price":
            return _price_history(start, end)
        if entity_id == "sensor.export_price":
            return _price_history(start, end, cheap=0.02, expensive=0.10)
        if entity_id == "sensor.combined_soc":
            return _flat_history(soc_pct, start, end)
        return []

    return _fetch


def _report(battery_kw):
    with patch.object(
        solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(battery_kw)
    ):
        return solver_writer._compute_report_for_window(
            _cfg(), DAY_START, DAY_END, allow_partial=True
        )


class TestTheShapeIsComplete(unittest.TestCase):
    def test_all_four_rows_carry_all_four_fields(self):
        result = _report(0.0)
        self.assertIsNotNone(result)
        dec = result["energy_decomposition"]
        self.assertEqual(sorted(dec), sorted(ROWS))
        for row in ROWS:
            with self.subTest(row=row):
                self.assertEqual(sorted(dec[row]), sorted(FIELDS))
                for f in FIELDS:
                    self.assertIsInstance(dec[row][f], float)


class TestItReconcilesWithTheAlreadyPublishedAchievedFigures(unittest.TestCase):
    """The achieved row must agree with `fleet_achieved_energy_in_kwh`/
    `_out_kwh` by construction. Two independently-derived answers to the
    same question is exactly the drift this repo has paid for before --
    so the reconciliation is asserted rather than assumed."""

    def test_sustained_discharge_matches_the_fleet_energy_fields(self):
        result = _report(20.0)
        self.assertIsNotNone(result)
        ach = result["energy_decomposition"]["achieved"]
        self.assertAlmostEqual(
            ach["discharge_kwh"], result["fleet_achieved_energy_out_kwh"], places=3
        )
        self.assertAlmostEqual(
            ach["charge_kwh"], result["fleet_achieved_energy_in_kwh"], places=3
        )
        # 20 kW for 24 h, and the sibling field is already pinned to 480
        # by test_quality_report_achieved_energy_and_reliability.py -- so
        # this also checks the pair agrees on a REAL number, not merely
        # with each other.
        self.assertAlmostEqual(ach["discharge_kwh"], 480.0, places=1)

    def test_sustained_charge_matches_the_fleet_energy_fields(self):
        result = _report(-20.0)
        self.assertIsNotNone(result)
        ach = result["energy_decomposition"]["achieved"]
        self.assertAlmostEqual(
            ach["charge_kwh"], result["fleet_achieved_energy_in_kwh"], places=3
        )
        self.assertAlmostEqual(
            ach["discharge_kwh"], result["fleet_achieved_energy_out_kwh"], places=3
        )
        self.assertAlmostEqual(ach["charge_kwh"], 480.0, places=1)


class TestTheGridFiguresFollowTheDocumentedSignConvention(unittest.TestCase):
    """`grid_kw` + = import, - = export -- the same convention the hourly
    rows use. With load pinned at 2.0 kW and solar at 0.0, each
    trajectory's grid flow is exactly `2.0 + battery_net`, so these are
    hand-calculable rather than whatever the code emits."""

    def test_the_idle_reference_imports_its_load_and_exports_nothing(self):
        result = _report(0.0)
        self.assertIsNotNone(result)
        ref = result["energy_decomposition"]["reference"]
        self.assertAlmostEqual(
            ref["grid_import_kwh"], FIXTURE_LOAD_KW * HOURS_IN_DAY, places=1
        )
        self.assertEqual(ref["grid_export_kwh"], 0.0)

    def test_the_idle_reference_never_moves_the_battery(self):
        # True by construction, and worth pinning: a reference row that
        # ever showed battery activity would mean the idle trajectory
        # had stopped being idle.
        ref = _report(20.0)["energy_decomposition"]["reference"]
        self.assertEqual(ref["charge_kwh"], 0.0)
        self.assertEqual(ref["discharge_kwh"], 0.0)

    def test_sustained_charge_shows_up_as_import_not_export(self):
        # battery_net = +20 (charge) on top of a 2 kW load -> 22 kW
        # import for 24 h, nothing exported all day.
        ach = _report(-20.0)["energy_decomposition"]["achieved"]
        self.assertAlmostEqual(
            ach["grid_import_kwh"], (FIXTURE_LOAD_KW + 20.0) * HOURS_IN_DAY, places=1
        )
        self.assertEqual(ach["grid_export_kwh"], 0.0)

    def test_sustained_discharge_shows_up_as_export_not_import(self):
        # battery_net = -20 (discharge) against a 2 kW load -> 18 kW
        # export for 24 h, nothing imported all day.
        ach = _report(20.0)["energy_decomposition"]["achieved"]
        self.assertAlmostEqual(
            ach["grid_export_kwh"], (20.0 - FIXTURE_LOAD_KW) * HOURS_IN_DAY, places=1
        )
        self.assertEqual(ach["grid_import_kwh"], 0.0)


class TestTheDeltaIsExactlyAchievedMinusOracle(unittest.TestCase):
    """The delta row is the whole point -- it is the line that reads
    "achieved charged 25 kWh more than the oracle" without anyone summing
    24 hourly rows by hand. If it ever drifts from the two rows it is
    derived from it is worse than absent, so it is checked against them
    directly rather than against an expected constant."""

    def test_every_field_is_the_difference_of_its_two_sources(self):
        for battery_kw in (0.0, 20.0, -20.0):
            with self.subTest(battery_kw=battery_kw):
                dec = _report(battery_kw)["energy_decomposition"]
                for f in FIELDS:
                    self.assertAlmostEqual(
                        dec["achieved_minus_oracle"][f],
                        round(dec["achieved"][f] - dec["oracle"][f], 3),
                        places=3,
                    )

    def test_a_day_where_achieved_moved_more_energy_than_the_oracle(self):
        # The real shape this field exists to surface: 20 kW of sustained
        # discharge is far beyond anything the LP's own 10 kW bound lets
        # the oracle do, so achieved MUST come out ahead on discharge --
        # a positive delta, stated once, rather than inferred from
        # cancelling hourly buckets.
        dec = _report(20.0)["energy_decomposition"]
        self.assertGreater(dec["achieved"]["discharge_kwh"], 0.0)
        self.assertGreater(dec["achieved_minus_oracle"]["discharge_kwh"], 0.0)


if __name__ == "__main__":
    unittest.main()
