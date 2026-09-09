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
            "epr_pct",
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
            "scored_participants",
        ):
            self.assertIn(key, report)

        # nimbus issue #585: this scorer has no battery_participant
        # awareness yet -- always exactly ["home"] until the real
        # multi-battery fix lands, never silently implying a broader
        # scope than what was actually scored.
        self.assertEqual(report["scored_participants"], ["home"])

        # epr_pct is the canonical 0..1 fraction scaled to a real percent
        # (0..100), locked to two decimals. The state channel and the
        # flattened Quality EPR child both publish it so the number
        # renders honestly against unit_of_measurement="%".
        self.assertAlmostEqual(report["epr_pct"], report["epr"] * 100, places=2)

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

    def test_hourly_reconstruction_dicts_are_row_major_by_iso_timestamp(self):
        """Locks the 2026-08-31 reframe of PR #297's reconstruction dicts:
        row-major, indexed by ISO local timestamp with the site tz offset
        ('2026-08-30T00:00:00+10:00' style for Brisbane), each row a self-
        describing record with the seven entity fields inside. Reframe
        rationale: the column-major shape (7 keys x 24 hour-strings)
        needed 7 attribute lookups per hour on the consumer side; the
        row-major shape is 1 lookup per hour and each row parses straight
        into a Date via `new Date(key)`.
        """
        cfg = _cfg()
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)

        for key in ("j_ref_hourly", "j_ach_hourly", "j_star_hourly"):
            self.assertIn(key, report)
            hourly = report[key]
            self.assertIsInstance(hourly, dict)
            # 24 rows, one per hour of yesterday.
            self.assertEqual(len(hourly), 24)
            expected_ts = [
                (YESTERDAY_START + timedelta(hours=h)).isoformat() for h in range(24)
            ]
            self.assertEqual(list(hourly.keys()), expected_ts)
            # Every top-level key must parse as an ISO tz-aware datetime
            # and equal yesterday's local hour anchor.
            for h, ts in enumerate(hourly):
                parsed = datetime.fromisoformat(ts)
                self.assertIsNotNone(parsed.tzinfo)
                self.assertEqual(parsed, YESTERDAY_START + timedelta(hours=h))
            # Every row is a dict with exactly the seven entity fields,
            # in the documented order.
            expected_fields = [
                "import_price_aud_per_kwh",
                "export_price_aud_per_kwh",
                "load_kw",
                "solar_kw",
                "battery_kw",
                "grid_kw",
                "soc_pct",
            ]
            for ts, row in hourly.items():
                self.assertIsInstance(row, dict)
                self.assertEqual(list(row.keys()), expected_fields)
                for field in expected_fields:
                    self.assertIsInstance(row[field], float)
            # Reconstruction identity holds every hour in every trajectory:
            # load - solar + battery = grid, exact by construction.
            for row in hourly.values():
                identity = (
                    row["load_kw"]
                    - row["solar_kw"]
                    + row["battery_kw"]
                    - row["grid_kw"]
                )
                self.assertAlmostEqual(identity, 0.0, places=4)

    def test_efficiency_is_sqrt_split_not_applied_directly(self):
        # Nimbus issue #168 (Mark Purcell, 2026-08-25): this used to pass
        # the round-trip solver_efficiency_percent straight through to
        # BOTH charge_efficiency and discharge_efficiency, modeling a
        # battery physically different from the one main()'s own real
        # live plan solves against (which sqrt()-splits it). Verifies
        # the actual BatteryConfig this function builds uses the
        # sqrt-split value, not the raw round-trip one.
        cfg = _cfg(solver_efficiency_percent=90.0)
        captured = {}
        real_battery_config = solver_writer.elements.BatteryConfig

        def spy(**kwargs):
            captured.update(kwargs)
            return real_battery_config(**kwargs)

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(solver_writer.elements, "BatteryConfig", side_effect=spy),
        ):
            solver_writer.compute_daily_quality_report(cfg, NOW)

        self.assertAlmostEqual(captured["charge_efficiency"], 90.0**0.5 / 10, places=6)
        self.assertAlmostEqual(
            captured["discharge_efficiency"], 90.0**0.5 / 10, places=6
        )

    def test_battery_config_uses_zero_salvage_value_not_the_configured_one(self):
        """Real, live-reported bug (2026-08-29/30): this function's own
        BatteryConfig used the configured solver_salvage_value (a flat
        rate meant for the live, multi-day FORWARD plan) to credit
        leftover end-of-day SoC when scoring an already-elapsed day.
        On a day where the real dispatch accidentally ended near-full
        (e.g. a disrupted P2P sell automation barely discharging that
        night), that credit -- flat OR a concave curve, both tried --
        over-rewarded the accidental full ending relative to what even
        a fully unconstrained perfect-foresight oracle could match,
        letting real-achieved beat the oracle: EPR>100%, negative
        regret_dollars. Verified against a real incident day: flat
        salvage gave 145.0%/-$18.15 (invalid), a concave curve gave
        127.7%/-$11.14 (still invalid), salvage_value=0.0 gave
        76.0%/+$8.94 (both valid).

        Locks in the real, structural fix: this scorer evaluates exactly
        ONE already-elapsed calendar day in isolation, so it must never
        credit leftover SoC via any positive per-kWh rate at all --
        salvage_value must be exactly 0.0 and terminal_value_breakpoints
        must be None, regardless of what solver_salvage_value is
        configured to (that value is for the live forward plan only).
        """
        cfg = _cfg(solver_salvage_value=0.12)
        captured = {}
        real_battery_config = solver_writer.elements.BatteryConfig

        def spy(**kwargs):
            captured.update(kwargs)
            return real_battery_config(**kwargs)

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fetch_side_effect,
            ),
            patch.object(solver_writer.elements, "BatteryConfig", side_effect=spy),
        ):
            solver_writer.compute_daily_quality_report(cfg, NOW)

        self.assertEqual(captured.get("salvage_value"), 0.0)
        self.assertIsNone(captured.get("terminal_value_breakpoints"))

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


class TestSocDiscrepancyStats(unittest.TestCase):
    """nimbus issue #427 (Mark Purcell): the achieved (J_ach) trajectory's
    SoC is *integrated* from real battery-power history through the
    round-trip efficiency model, never read from the real SoC sensor --
    a real, sometimes large, source of divergence from ground truth that
    compounds over a scored day (drift, calibration error, sensor faults
    all show up here but nowhere else, since j_ach_hourly's own soc_pct
    is the integrated value, not a measurement). Proves
    soc_discrepancy_max_pct/soc_discrepancy_mean_pct genuinely reflect a
    real, hand-computable divergence between the two -- not just always
    None or 0.
    """

    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            # Battery never touched -- the achieved trajectory's own
            # integrated SoC stays exactly flat at its initial value
            # (50%) for the whole day, a known, exact baseline to
            # diverge the real sensor reading away from below.
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        if entity_id == "sensor.real_soc":
            # Real recorder SoC drifting away from the flat 50% the
            # achieved trajectory stays pinned to (h=0 -> 50%, +2 real
            # percentage points per hour) -- a deliberate, exactly
            # hand-computable divergence: max gap 46.0pp at h=23, mean
            # gap 23.0pp across the 24 hourly buckets (mean of the
            # arithmetic sequence 0, 2, 4, ..., 46).
            return [
                (YESTERDAY_START + timedelta(hours=h), 50.0 + 2.0 * h)
                for h in range(24)
            ]
        return []

    def test_real_soc_divergence_is_captured(self):
        cfg = _cfg(solver_battery_soc_sensor="sensor.real_soc")
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        self.assertIn("soc_discrepancy_max_pct", report)
        self.assertIn("soc_discrepancy_mean_pct", report)
        self.assertAlmostEqual(report["soc_discrepancy_max_pct"], 46.0, places=2)
        self.assertAlmostEqual(report["soc_discrepancy_mean_pct"], 23.0, places=2)

    def test_no_soc_sensor_configured_returns_none_not_zero(self):
        # cfg's solver_battery_soc_sensor is unset (matches every other
        # test in this file) -- soc_hist is genuinely empty, so both new
        # fields must honestly report None, never a fabricated 0.0.
        cfg = _cfg()
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        self.assertIsNone(report["soc_discrepancy_max_pct"])
        self.assertIsNone(report["soc_discrepancy_mean_pct"])
        self.assertIsNone(report["soc_discrepancy_reliable"])

    def test_in_range_divergence_within_threshold_is_reliable(self):
        """The already-existing real_soc_divergence test above never
        leaves [0, 100] on either side (50%/flat vs 50-96%/real) --
        adding the range-only reliability field must not disturb that
        already-correct, genuinely-large (46pp) discrepancy report, so
        long as the agreement thresholds are set wide enough to accept
        it (nimbus issue #538's own default thresholds, 15pt max/8pt
        mean, do NOT accept a 46pp/23pp gap -- see the sibling test
        below for that real, intended behaviour change)."""
        cfg = _cfg(
            solver_battery_soc_sensor="sensor.real_soc",
            solver_soc_discrepancy_max_threshold_pct=100.0,
            solver_soc_discrepancy_mean_threshold_pct=100.0,
        )
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        self.assertTrue(report["soc_discrepancy_reliable"])
        self.assertIsNone(report["soc_discrepancy_reason"])
        self.assertAlmostEqual(report["soc_discrepancy_max_pct"], 46.0, places=2)

    def test_in_range_divergence_beyond_the_default_threshold_is_unreliable(self):
        """nimbus issue #538 (Mark Purcell, real household finding): the
        exact behaviour the range-only test above USED TO assert was
        the bug -- a 46pp max / 23pp mean gap against the real SoC
        sensor is a genuinely large, sustained disagreement, and never
        leaving [0, 100] does not make it trustworthy. With the default
        thresholds (15pt max / 8pt mean, nothing overridden), this same
        scenario is now correctly unreliable with reason="disagreement",
        not "out_of_range" (the trajectory never left the physical
        range at any point)."""
        cfg = _cfg(solver_battery_soc_sensor="sensor.real_soc")
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        self.assertFalse(report["soc_discrepancy_reliable"])
        self.assertEqual(report["soc_discrepancy_reason"], "disagreement")
        self.assertAlmostEqual(report["soc_discrepancy_max_pct"], 46.0, places=2)


class TestSocDiscrepancyOutOfRangeClampAndReliableFlag(unittest.TestCase):
    """nimbus issue #445 (Mark Purcell): a live install with a battery-
    power sensor whose real recorder history only covers the last few
    hours of the scored 24h window (a newly-configured sensor, or an
    outage) integrates a fabricated delta for every hour before that --
    confirmed live, this produced a physically-impossible
    soc_discrepancy_max_pct of 327.67%. Two independently-bounded
    [0, 100] percentages can never legitimately disagree by more than
    100 points, so _soc_discrepancy_stats() itself (unit-tested directly
    here, not through the full pipeline -- precedent already established
    elsewhere in this test suite for solver_writer's other private
    functions) must both (a) clamp the reported gap to something
    physically meaningful and (b) flag the out-of-range condition
    explicitly rather than let a caller infer it from the magnitude
    alone.
    """

    def test_ach_pct_above_100_is_clamped_and_flagged_unreliable(self):
        # A real out-of-range achieved SoC (327% -- the exact live figure
        # from the issue) against an in-range real reading (60%): the raw
        # gap would be 267pp (impossible); clamped, it's exactly 40pp
        # (327 clamped to 100, minus 60).
        soc_hist = [(YESTERDAY_START, 60.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": 327.67}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertEqual(result["soc_discrepancy_max_pct"], 40.0)
        self.assertFalse(result["soc_discrepancy_reliable"])

    def test_ach_pct_below_zero_is_clamped_and_flagged_unreliable(self):
        soc_hist = [(YESTERDAY_START, 20.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": -85.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        # -85 clamped to 0, minus the real 20 -> 20pp, never 105pp.
        self.assertEqual(result["soc_discrepancy_max_pct"], 20.0)
        self.assertFalse(result["soc_discrepancy_reliable"])

    def test_result_never_exceeds_100_even_with_both_sides_out_of_range(self):
        soc_hist = [(YESTERDAY_START, -40.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": 500.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertLessEqual(result["soc_discrepancy_max_pct"], 100.0)
        self.assertFalse(result["soc_discrepancy_reliable"])

    def test_all_in_range_values_stay_reliable_true(self):
        soc_hist = [(YESTERDAY_START, 55.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": 50.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertEqual(result["soc_discrepancy_max_pct"], 5.0)
        self.assertTrue(result["soc_discrepancy_reliable"])


class TestSocDiscrepancyRealBoundaryEdgeTolerance(unittest.TestCase):
    """nimbus issue #571 (Mark Purcell): confirmed against real recorder
    data 8 Sep -- a pack that genuinely runs down to its own physical
    cut-off (real SoC sensor reads 0.0%, verified against the plant's own
    discharge_cut_off_soc, not a sensor fault) can still leave the
    achieved trajectory's own round-trip-efficiency integration a few
    points negative for that same hour (Mark's own real numbers: -2.3%
    raw, about -9% with efficiency applied). That is integration loss
    landing at a real physical edge, not evidence the two sensors
    describe different storage, and should not flag the day unreliable
    on its own. See _SOC_BOUNDARY_EDGE_TOLERANCE_PCT's own comment for
    why this is a separate, fixed concept from the disagreement
    thresholds tested above.
    """

    def test_ach_pct_slightly_negative_with_real_at_zero_is_not_out_of_range(self):
        soc_hist = [(YESTERDAY_START, 0.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": -9.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertTrue(result["soc_discrepancy_reliable"])
        self.assertIsNone(result["soc_discrepancy_reason"])
        # -9.0 clamps to 0.0, same as the real 0.0 -- zero gap, exactly
        # what "genuinely at the physical edge on both sides" should read.
        self.assertEqual(result["soc_discrepancy_max_pct"], 0.0)

    def test_ach_pct_slightly_over_100_with_real_at_100_is_not_out_of_range(self):
        soc_hist = [(YESTERDAY_START, 100.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": 104.5}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertTrue(result["soc_discrepancy_reliable"])
        self.assertIsNone(result["soc_discrepancy_reason"])

    def test_real_just_outside_the_edge_tolerance_still_flags_out_of_range(self):
        # real_pct=2.0 is more than _SOC_BOUNDARY_EDGE_TOLERANCE_PCT (1.0)
        # away from 0 -- this is NOT the "genuinely at the physical edge"
        # case, so the achieved trajectory's own excursion below 0 still
        # needs explaining and stays flagged.
        soc_hist = [(YESTERDAY_START, 2.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": -9.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertFalse(result["soc_discrepancy_reliable"])
        self.assertEqual(result["soc_discrepancy_reason"], "out_of_range")

    def test_real_sensor_itself_out_of_range_still_flags_regardless_of_edge(self):
        # The real sensor reading -5.0 is itself impossible -- a genuine
        # sensor fault, not a real physical edge -- so this must still
        # flag even though it's near the same boundary the achieved
        # trajectory also crossed.
        soc_hist = [(YESTERDAY_START, -5.0)]
        j_ach_hourly = {YESTERDAY_START.isoformat(): {"soc_pct": -9.0}}
        result = solver_writer._soc_discrepancy_stats(soc_hist, j_ach_hourly)
        self.assertFalse(result["soc_discrepancy_reliable"])
        self.assertEqual(result["soc_discrepancy_reason"], "out_of_range")


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


class TestResampleHistoryMean(unittest.TestCase):
    """nimbus issue #428 (Mark Purcell): resample_history_mean() averages
    every real sample inside [grid_time, grid_time + period_hours),
    instead of resample_history_nearest()'s single nearest-at-or-before
    instant -- proves a brief spike gets diluted by the rest of the real
    period's samples, matching Mark's own real recorder finding (a
    +20.939kW spike at exactly a period boundary against a real hourly
    mean of -2.66kW).
    """

    def test_uniform_points_average_within_the_window(self):
        anchor = YESTERDAY_START
        pts = [
            (anchor, 1.0),
            (anchor + timedelta(minutes=5), 2.0),
            (anchor + timedelta(minutes=10), 3.0),
            # Outside this period's own [anchor, anchor+15min) window --
            # must not be pulled in.
            (anchor + timedelta(minutes=20), 100.0),
        ]
        self.assertEqual(
            solver_writer.resample_history_mean(pts, [anchor], period_hours=0.25),
            [2.0],
        )

    def test_brief_spike_is_diluted_not_treated_as_representative(self):
        # Mark's own real shape: one brief spike sample right at the top
        # of the period, followed by a real, stable, opposite-sign
        # reading for the rest of the period -- resample_history_nearest
        # would return the spike alone (whatever the FIRST sample at/
        # before the grid instant is); the mean must instead land close
        # to the stable reading, not the spike.
        anchor = YESTERDAY_START + timedelta(hours=4)  # the real 04:00 case
        pts = [
            (anchor, 20.939),  # the real, brief, unrepresentative spike
            (anchor + timedelta(minutes=1), -2.6),
            (anchor + timedelta(minutes=5), -2.7),
            (anchor + timedelta(minutes=10), -2.6),
        ]
        mean = solver_writer.resample_history_mean(pts, [anchor], period_hours=0.25)[0]
        nearest = solver_writer.resample_history_nearest(pts, [anchor])[0]
        self.assertEqual(nearest, 20.939)  # confirms the bug mechanism itself
        self.assertLess(mean, 5.0)  # the mean is nowhere near the spike
        self.assertAlmostEqual(mean, (20.939 - 2.6 - 2.7 - 2.6) / 4, places=6)

    def test_falls_back_to_nearest_when_period_has_no_real_samples(self):
        # Sparse/low-frequency history -- e.g. a sensor that only reports
        # on real change, and didn't change at all during this period.
        anchor = YESTERDAY_START
        pts = [(anchor - timedelta(hours=2), 7.0)]
        self.assertEqual(
            solver_writer.resample_history_mean(pts, [anchor], period_hours=0.25),
            [7.0],
        )

    def test_empty_history_returns_default(self):
        grid = [YESTERDAY_START]
        self.assertEqual(
            solver_writer.resample_history_mean(
                [], grid, period_hours=0.25, default=0.42
            ),
            [0.42],
        )


class TestAchievedTrajectoryUsesPeriodMeanNotSpike(unittest.TestCase):
    """End-to-end version of the #428 fix: reconstructs Mark's own real
    04:00 scenario through compute_daily_quality_report() itself and
    proves the achieved trajectory's grid direction for that hour follows
    the real period mean (net export, matching the real meter), not the
    single unrepresentative spike sample (which would reconstruct to net
    import) resample_history_nearest() used to pick up.
    """

    def _fetch_side_effect(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_load":
            return _flat_history(1.058, YESTERDAY_START, YESTERDAY_END)
        if entity_id == "sensor.real_battery":
            # 1-minute-resolution flat -2.66kW (raw convention, positive=
            # discharge, this project's default) all day -- real enough
            # sample density that a single brief spike can't dominate a
            # period's mean -- except a brief spike to +20.939 right at
            # 04:00:00, Mark's own real shape.
            out = _flat_history(-2.66, YESTERDAY_START, YESTERDAY_END, step_minutes=1)
            spike_time = YESTERDAY_START + timedelta(hours=4)
            out = [(t, v) for t, v in out if t != spike_time]
            out.append((spike_time, 20.939))
            return sorted(out, key=lambda x: x[0])
        if entity_id == "sensor.import_price":
            return _price_history(YESTERDAY_START)
        if entity_id == "sensor.export_price":
            return _price_history(YESTERDAY_START, cheap=0.02, expensive=0.10)
        return []

    def test_hour_04_grid_direction_matches_the_real_mean_not_the_spike(self):
        # Matches Mark's own real install convention (issue #299/#388):
        # raw sensor reports positive=charge, so the +20.939 spike is a
        # charge event and the flat -2.66 baseline is a real discharge --
        # same literal numbers he reported.
        cfg = _cfg(solver_battery_power_positive_is_charge=True)
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fetch_side_effect,
        ):
            report = solver_writer.compute_daily_quality_report(cfg, NOW)
        self.assertIsNotNone(report)
        row = report["j_ach_hourly"][(YESTERDAY_START + timedelta(hours=4)).isoformat()]
        # grid_kw sign convention: + = import, - = export (see
        # _hourly_reconstruction_dicts_are_row_major_by_iso_timestamp's
        # own docstring). Real meter/period-mean direction is export
        # (negative) -- load 1.058 - solar 0 + battery_net(mean, positive
        # =charge) should stay close to the flat -2.66 discharge case,
        # not swing to a large positive import the raw spike alone would
        # produce.
        self.assertLess(row["grid_kw"], 0.0)


class TestPublishDailyQualityReport(unittest.TestCase):
    def test_already_scored_yesterday_skips_recompute_but_still_repushes(self):
        """Real fix (2026-08-30, issues #289/#292): the fast path must
        still re-push the SAME already-read state/attributes -- skipping
        the expensive recompute but ALSO skipping the publish entirely
        (the old, buggy behaviour this test used to assert) is exactly
        what let this entity's own freshness stamp go stale and get
        marked unavailable, over and over."""
        cfg = _cfg()
        existing = {"state": "0.75", "attributes": {"latest_date": "2026-08-24"}}
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(solver_writer, "compute_daily_quality_report") as compute,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_daily_quality_report(cfg, NOW)
        compute.assert_not_called()
        post.assert_called_once_with(
            solver_writer.QUALITY_ENTITY_ID, existing["state"], existing["attributes"]
        )

    def test_not_yet_scored_computes_and_pushes(self):
        cfg = _cfg()
        # Shape matches compute_daily_quality_report()'s real return dict:
        # epr is the canonical 0..1 fraction, epr_pct is the same value
        # scaled to a real percent (0..100) for the state channel so it
        # renders honestly against unit_of_measurement="%".
        day_entry = {
            "epr": 0.5,
            "epr_pct": 50.0,
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
        # State is the percent-scaled value (0..100) so it renders honestly
        # against unit_of_measurement="%" (the frontend would otherwise
        # display 0.5 with a "%" suffix as "0.5 %", the real bug this PR
        # fixes).
        self.assertEqual(state, 50.0)
        self.assertEqual(attrs["latest_date"], "2026-08-24")
        # Both fields are preserved on the attribute dict via **day_entry:
        # epr is the canonical 0..1 fraction for downstream consumers,
        # epr_pct is the same value scaled to a percent.
        self.assertEqual(attrs["epr"], 0.5)
        self.assertEqual(attrs["epr_pct"], 50.0)

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
