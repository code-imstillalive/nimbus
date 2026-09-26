"""Regression tests for nimbus issues #532/#533 (Mark Purcell, real
household data, 7 Sep 2026): a combined battery-power sensor summing the
home pack and a shared EV DC-charger channel fed a 100 kWh single-battery
model, driving the achieved SoC integration outside [0, 100] for a whole
day with genuinely complete recorder history -- not the #445 partial-
history case _soc_discrepancy_stats() already handled. Two real asks:

- #532: expose achieved_energy_in_kwh/achieved_energy_out_kwh on the
  report so a household can compare against their own configured
  solver_battery_capacity_kwh and tell a history gap from a sensor
  covering more physical storage than the capacity figure describes,
  without a manual recorder pull.
- #533: soc_discrepancy_reliable used to live only as a parent
  attribute, invisible to a consumer reading the flattened EPR sensor
  alone -- epr_reliable is the same value, named for what it qualifies.
  publish_daily_quality_report() also logs once per scored day (not
  every cycle) when a day's own report is unreliable.

Uses the same real _compute_report_for_window()/fixture pattern as the
sibling test_compute_report_for_window.py / test_quality_report_soc_
clamp.py files -- exercises the real function, not a reimplementation.
"""

from __future__ import annotations

import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import battery_participants as battery_participants_inputs

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


def _make_fetch(battery_kw, soc_pct=50.0):
    """Same solvable scenario the sibling window tests use, with the
    battery-power series pinned to one constant value (positive =
    discharge, this project's own established convention) so a test can
    drive real, sustained battery activity independent of the LP's own
    configured max_charge_kw/max_discharge_kw bounds -- exactly Mark's
    own real-world case, where the recorded history reflects real
    hardware/summed-sensor readings the configured model never bounds.
    """

    def _fetch(entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, start, end)
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


class TestAchievedEnergyInOut(unittest.TestCase):
    def test_zero_battery_activity_gives_zero_both_ways(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(0.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["achieved_energy_in_kwh"], 0.0)
        self.assertEqual(result["achieved_energy_out_kwh"], 0.0)

    def test_sustained_discharge_integrates_to_energy_out_only(self):
        # 20 kW discharge for the full 24h window -- 480 kWh out, 0 kWh
        # in. Deliberately far beyond the configured 50 kWh capacity
        # (Mark's own real case: a combined sensor moved >100 kWh
        # through a 100 kWh model in one day).
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(20.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["achieved_energy_out_kwh"], 480.0, places=1)
        self.assertEqual(result["achieved_energy_in_kwh"], 0.0)

    def test_sustained_charge_integrates_to_energy_in_only(self):
        # -20 kW (charge) for the full window -- symmetric to the above.
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(-20.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["achieved_energy_in_kwh"], 480.0, places=1)
        self.assertEqual(result["achieved_energy_out_kwh"], 0.0)


class TestEprReliableMirrorsSocDiscrepancyReliable(unittest.TestCase):
    def test_a_well_behaved_day_is_reliable_and_epr_reliable_matches(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(0.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertTrue(result["soc_discrepancy_reliable"])
        self.assertEqual(result["epr_reliable"], result["soc_discrepancy_reliable"])

    def test_532s_own_real_case_a_sustained_large_discharge_is_unreliable(self):
        # A real, physically-impossible-for-the-configured-model
        # scenario: 20 kW sustained discharge against a 50 kWh capacity
        # drives the achieved SoC integration deep outside [0, 100]
        # while the (flat, well-behaved) real SoC sensor stays in range
        # -- exactly #532's own "combined sensor covers more storage
        # than capacity describes" shape, reproduced synthetically.
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(20.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertFalse(result["soc_discrepancy_reliable"])
        self.assertEqual(result["epr_reliable"], result["soc_discrepancy_reliable"])
        self.assertFalse(result["epr_reliable"])


class TestSocDiscrepancyAgreementThreshold(unittest.TestCase):
    """nimbus issue #538 (Mark Purcell, real household finding on this
    repo's own v0.94.166): soc_discrepancy_reliable used to test ONLY
    whether the achieved SoC integration stayed inside [0, 100] --
    blind to a genuinely large, sustained disagreement that never
    leaves that range (his own real case: raising the configured
    battery capacity kept the trajectory in-range while the gap against
    the real SoC sensor stayed at 40.7 points max / 10.85 mean, and the
    flag still read "reliable"). These tests reproduce that shape
    synthetically: a bounded 3kW/3h discharge burst against a flat 50%
    real SoC sensor settles ~18 points away and NEVER leaves [0, 100]
    at any point in the window (unlike TestEprReliableMirrorsSocDiscrepancyReliable's
    own 20kW/24h scenario, which leaves range almost immediately)."""

    def _fetch_bounded_disagreement(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, start, end)
        if entity_id == "sensor.real_battery":
            mid = start + timedelta(hours=3)
            return _flat_history(3.0, start, mid) + _flat_history(0.0, mid, end)
        if entity_id == "sensor.import_price":
            return _price_history(start, end)
        if entity_id == "sensor.export_price":
            return _price_history(start, end, cheap=0.02, expensive=0.10)
        if entity_id == "sensor.combined_soc":
            return _flat_history(50.0, start, end)
        return []

    def test_a_bounded_in_range_disagreement_is_unreliable_by_default(self):
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_bounded_disagreement,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        # Real value, confirmed via direct computation: max ~18.5pt, mean
        # ~17.5pt -- both exceed the 15/8pt defaults, neither the day nor
        # any single hour ever left [0, 100] (a genuinely different
        # scenario from the 20kW/24h out-of-range case above).
        self.assertGreater(result["soc_discrepancy_max_pct"], 15.0)
        self.assertGreater(result["soc_discrepancy_mean_pct"], 8.0)
        self.assertFalse(result["soc_discrepancy_reliable"])
        self.assertEqual(result["soc_discrepancy_reason"], "disagreement")
        self.assertFalse(result["epr_reliable"])

    def test_a_household_can_raise_its_own_configured_threshold(self):
        # Same exact scenario, but this household's own dashboard
        # numbers (number.nimbus_solver_soc_discrepancy_max_threshold_pct
        # / _mean_threshold_pct) have been tuned up to accept a noisier
        # install -- #538's own item 1 ask, end-to-end through cfg.
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_bounded_disagreement,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(
                    solver_soc_discrepancy_max_threshold_pct=50.0,
                    solver_soc_discrepancy_mean_threshold_pct=50.0,
                ),
                DAY_START,
                DAY_END,
                allow_partial=True,
            )
        self.assertIsNotNone(result)
        self.assertTrue(result["soc_discrepancy_reliable"])
        self.assertIsNone(result["soc_discrepancy_reason"])
        self.assertTrue(result["epr_reliable"])

    def test_out_of_range_reason_takes_priority_over_disagreement(self):
        # #532's own real case (reused from TestEprReliableMirrors...
        # above): a 20kW/24h sustained discharge leaves [0, 100] almost
        # immediately -- reason must say "out_of_range", not
        # "disagreement", even though the raw gap numbers would also
        # exceed the agreement thresholds.
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(20.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertFalse(result["soc_discrepancy_reliable"])
        self.assertEqual(result["soc_discrepancy_reason"], "out_of_range")

    def test_a_well_behaved_day_has_no_reason(self):
        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(0.0)
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )
        self.assertIsNotNone(result)
        self.assertTrue(result["soc_discrepancy_reliable"])
        self.assertIsNone(result["soc_discrepancy_reason"])


class TestPublishLogsOncePerScoredDay(unittest.TestCase):
    """nimbus issue #533 item 3: the same #313/#314 'log once, with the
    number' discipline this project already applies elsewhere -- a
    day's own unreliable score must warn exactly once, not every cycle
    publish_daily_quality_report() happens to run."""

    def setUp(self):
        self._orig_warned = set(solver_writer._QUALITY_REPORT_UNRELIABLE_WARNED)
        solver_writer._QUALITY_REPORT_UNRELIABLE_WARNED.clear()

    def tearDown(self):
        solver_writer._QUALITY_REPORT_UNRELIABLE_WARNED.clear()
        solver_writer._QUALITY_REPORT_UNRELIABLE_WARNED.update(self._orig_warned)

    def _publish(self, now, battery_kw):
        # ha_get() raising is the "never scored before" fast-path miss --
        # matches every other publish_daily_quality_report() test's own
        # way of forcing the real compute path instead of the cached
        # re-push fast path.
        with (
            patch.object(
                solver_writer,
                "ha_get",
                side_effect=urllib.error.URLError("no cache"),
            ),
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_make_fetch(battery_kw),
            ),
            patch.object(solver_writer, "ha_post_state"),
        ):
            solver_writer.publish_daily_quality_report(_cfg(), now)

    def test_an_unreliable_day_warns_exactly_once_across_two_publishes(self):
        now = DAY_END  # scores DAY_START..DAY_END as "yesterday"
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as first:
            self._publish(now, 20.0)
        self.assertTrue(
            any("soc_discrepancy_reliable=False" in r.message for r in first.records)
        )
        # A second publish for the SAME scored day must not warn again --
        # ha_get() is mocked to always miss the fast-path cache here
        # (forcing a real recompute each time), isolating this test to
        # the log-dedup behaviour itself rather than the separate
        # fast-path re-push mechanism.
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="WARNING"),
        ):
            self._publish(now, 20.0)

    def test_a_reliable_day_never_warns(self):
        now = DAY_END
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="WARNING"),
        ):
            self._publish(now, 0.0)

    def test_the_warning_names_out_of_range_as_the_reason(self):
        # nimbus issue #538 item 2: the WARNING must name WHICH test
        # failed, not a single message that always reads as the
        # out-of-range case.
        now = DAY_END
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            self._publish(now, 20.0)
        self.assertTrue(
            any("reason=out_of_range" in r.message for r in captured.records)
        )

    def test_the_warning_names_disagreement_as_the_reason(self):
        with (
            patch.object(
                solver_writer, "ha_get", side_effect=urllib.error.URLError("no cache")
            ),
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=TestSocDiscrepancyAgreementThreshold()._fetch_bounded_disagreement,
            ),
            patch.object(solver_writer, "ha_post_state"),
            self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured,
        ):
            solver_writer.publish_daily_quality_report(_cfg(), DAY_END)
        self.assertTrue(
            any("reason=disagreement" in r.message for r in captured.records)
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------
# nimbus issue #858 (2026-09-14): achieved_energy_in_kwh/out_kwh are
# home-battery-only, while EPR/regret/SoC on the SAME sensor are
# fleet-wide. Found while answering Mark Purcell's #842 report, where the
# two fields stayed sane through #843's ~1,500 kW participant corruption
# -- which is only possible because participant data never enters them.
#
# These tests pin BOTH halves of the contract: the home-only pair keeps
# its exact original meaning (it is a real diagnostic against the
# configured solver_battery_power_sensor/capacity, not an oversight), and
# the new fleet_* / per-battery fields cover the whole scored fleet.
# ---------------------------------------------------------------------

_N_PERIODS_HINT = 96


def _participant(name, charge_kw, discharge_kw, n_periods):
    """One (BatteryConfig, actual_charge_kw, actual_discharge_kw,
    final_soc_kwh, soc_hist) tuple shaped exactly like
    _resolve_battery_participant_history() returns, with flat power
    arrays so the expected energy is trivially hand-checkable. soc_hist
    is deliberately empty -- this class's own tests are about the
    achieved_energy_*/fleet_achieved_energy_* fields, never soc_
    discrepancy_*, so there is no real SoC series to fake here; an empty
    history is correctly dropped from the SoC blend rather than faked."""
    import numpy as np
    from solver import elements

    cfg = elements.BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=3.0,
        max_soc_kwh=60.0,
        max_charge_kw=11.0,
        max_discharge_kw=11.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.1,
    )
    return (
        cfg,
        np.full(n_periods, float(charge_kw)),
        np.full(n_periods, float(discharge_kw)),
        30.0,
        [],
    )


class TestFleetAchievedEnergy(unittest.TestCase):
    def _run(self, battery_kw, participants=None):
        """Run the real window function, optionally injecting participants
        at the same seam _resolve_battery_participant_history() occupies."""

        def _resolve(*, n_periods, **_kw):
            if not participants:
                return []
            return [_participant(n, c, d, n_periods) for n, c, d in participants]

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_make_fetch(battery_kw),
            ),
            patch.object(
                battery_participants_inputs,
                "_resolve_battery_participant_history",
                side_effect=_resolve,
            ),
        ):
            return solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=True
            )

    def test_no_participants_fleet_equals_home(self):
        """Every install before #563, and every standalone/cron run, has
        no participants -- the new fields must be equal to the old ones by
        construction there, so nothing changes for those installs."""
        result = self._run(20.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            result["fleet_achieved_energy_out_kwh"],
            result["achieved_energy_out_kwh"],
            places=6,
        )
        self.assertAlmostEqual(
            result["fleet_achieved_energy_in_kwh"],
            result["achieved_energy_in_kwh"],
            places=6,
        )

    def test_no_participants_breakdown_has_exactly_the_home_battery(self):
        result = self._run(20.0)
        breakdown = result["achieved_energy_by_battery"]
        self.assertEqual(len(breakdown), 1)
        only = next(iter(breakdown.values()))
        self.assertAlmostEqual(only["out_kwh"], result["achieved_energy_out_kwh"], 6)
        self.assertAlmostEqual(only["in_kwh"], result["achieved_energy_in_kwh"], 6)

    def test_participants_are_included_in_fleet_totals(self):
        # Home discharges 20 kW all day (480 kWh out). Two EVs each
        # charge at a flat 5 kW all day (120 kWh in each).
        result = self._run(20.0, participants=[("ev_a", 5.0, 0.0), ("ev_b", 5.0, 0.0)])
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["fleet_achieved_energy_out_kwh"], 480.0, places=1)
        self.assertAlmostEqual(result["fleet_achieved_energy_in_kwh"], 240.0, places=1)

    def test_participants_do_not_change_the_home_only_pair(self):
        """The whole point of keeping the original fields: adding
        participants must not silently redefine a number a household
        already diagnoses against solver_battery_capacity_kwh."""
        without = self._run(20.0)
        with_ = self._run(20.0, participants=[("ev_a", 5.0, 0.0), ("ev_b", 5.0, 0.0)])
        self.assertAlmostEqual(
            with_["achieved_energy_out_kwh"], without["achieved_energy_out_kwh"], 6
        )
        self.assertAlmostEqual(
            with_["achieved_energy_in_kwh"], without["achieved_energy_in_kwh"], 6
        )
        # ...and the home-only figure genuinely differs from the fleet one
        # once participants exist, which is the bug this issue is about.
        self.assertNotAlmostEqual(
            with_["achieved_energy_in_kwh"],
            with_["fleet_achieved_energy_in_kwh"],
            places=1,
        )

    def test_breakdown_attributes_energy_to_the_right_battery(self):
        result = self._run(20.0, participants=[("ev_a", 5.0, 0.0), ("ev_b", 2.0, 0.0)])
        breakdown = result["achieved_energy_by_battery"]
        self.assertEqual(len(breakdown), 3)
        self.assertIn("ev_a", breakdown)
        self.assertIn("ev_b", breakdown)
        self.assertAlmostEqual(breakdown["ev_a"]["in_kwh"], 120.0, places=1)
        self.assertAlmostEqual(breakdown["ev_b"]["in_kwh"], 48.0, places=1)
        self.assertAlmostEqual(breakdown["ev_a"]["out_kwh"], 0.0, places=6)

    def test_a_corrupt_participant_is_attributable_from_the_sensor_alone(self):
        """#843's own failure shape: one participant carrying physically
        impossible power. Before #858 the report showed a sane home-only
        energy figure and no way to attribute the blow-up; now the
        breakdown names the offending battery directly."""
        result = self._run(
            20.0, participants=[("ev_good", 5.0, 0.0), ("ev_bad", 1500.0, 0.0)]
        )
        breakdown = result["achieved_energy_by_battery"]
        self.assertAlmostEqual(breakdown["ev_good"]["in_kwh"], 120.0, places=1)
        self.assertGreater(breakdown["ev_bad"]["in_kwh"], 30000.0)
        # The home-only field stays sane -- exactly what Mark observed on
        # #842, and now explainable from the same sensor's attributes.
        self.assertEqual(result["achieved_energy_in_kwh"], 0.0)
