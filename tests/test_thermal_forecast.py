"""nimbus issue #592: real tests for thermal_forecast.py -- pure Python,
no HA imports, same "test the module directly" convention as
test_done_condition.py."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load")
)
import thermal_forecast as tf

_TZ = timezone(timedelta(hours=10))


class TestLearnThermalRates(unittest.TestCase):
    def test_empty_history_falls_back_to_the_534_cited_defaults(self):
        rates = tf.learn_thermal_rates([], on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )
        self.assertEqual(rates.idle_decay_c_per_hour, tf.DEFAULT_IDLE_DECAY_C_PER_HOUR)
        self.assertFalse(rates.is_learned)

    def test_heating_rate_is_learned_from_the_idle_to_idle_pair(self):
        # nimbus issue #610 (Mark Purcell, real finding: current_temperature
        # reads ~10-11 degC LOW while the compressor runs on the #534
        # bridge -- readings taken DURING a heating segment are never
        # trustworthy, only the idle readings bracketing it are). Last
        # idle-before reading 48.0 degC; the heating segment's own
        # readings are deliberately implausible/depressed (46.6, 41.0)
        # to prove they are never used; the settled idle-after reading
        # (>= 5 min after the run stops) is 61.0 degC -- a real 13 degC
        # gain the OLD (pre-#610) direct-segment-boundary computation
        # could never see correctly on a device like this one.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        heating_end = t0 + timedelta(hours=2, minutes=44)
        history = [
            (t0, 48.0, 0.0),  # idle-before -- the real, trustworthy anchor
            (
                t0 + timedelta(minutes=10),
                46.6,
                0.6,
            ),  # heating start (depressed, unused)
            (heating_end, 41.0, 0.6),  # heating end (depressed, unused)
            (
                heating_end + timedelta(minutes=3),
                55.0,
                0.0,
            ),  # not yet settled -- skipped
            (
                heating_end + timedelta(minutes=6),
                61.0,
                0.0,
            ),  # settled -- the real anchor
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        # is_learned requires BOTH a heating and an idle-decay sample --
        # this fixture is deliberately narrow (proving only the heating-
        # rate pairing), so it checks heating_sample_count directly
        # rather than the combined flag (see the sibling decay-only test
        # for why the two are asserted separately).
        self.assertEqual(rates.heating_sample_count, 1)
        # 13 degC gained over 0.6kW * 2h44min (1.64kWh) -> ~7.9 degC/kWh,
        # in the ballpark of #592's own cited ~8 degC/kWh, not the raw
        # default, and NOT the wrong-by-an-order-of-magnitude number the
        # old (pre-#610) direct-boundary computation would have produced
        # from the depressed in-run readings (41.0 -> 46.6 is a FALL).
        self.assertGreater(rates.heating_rate_c_per_kwh, 5.0)
        self.assertLess(rates.heating_rate_c_per_kwh, 10.0)

    def test_idle_decay_rate_is_learned_from_a_clean_idle_segment(self):
        # No heating at all -- isolates idle decay from the idle-to-idle
        # heating-rate pairing above, since a single contiguous idle run
        # spanning a heating event's own "settling" period would mix
        # catch-up movement into what should be pure decay.
        t0 = datetime(2026, 9, 6, 8, 0, tzinfo=_TZ)
        history = [
            (t0, 60.0, 0.0),
            (t0 + timedelta(hours=1), 59.5, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertFalse(rates.is_learned)  # no heating segment at all this cycle
        self.assertEqual(rates.idle_sample_count, 1)
        self.assertAlmostEqual(rates.idle_decay_c_per_hour, 0.5)

    def test_heating_segment_with_no_idle_segment_on_either_side_is_dropped(self):
        # The very first/last segment in a fetched history window has no
        # real idle segment to pair with on one side -- must be dropped,
        # not learned from a one-sided or missing anchor.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (
                t0,
                46.6,
                0.6,
            ),  # heating starts at the very first sample -- no idle-before
            (t0 + timedelta(minutes=20), 41.0, 0.6),
            (t0 + timedelta(minutes=25), 55.0, 0.0),
            (t0 + timedelta(minutes=35), 61.0, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(rates.heating_sample_count, 0)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )

    def test_heating_segment_whose_idle_after_never_settles_is_dropped(self):
        # The fetched history window ends before the idle-after segment
        # reaches a sample SETTLING_MINUTES past the run's own end --
        # must not learn from a still-catching-up reading.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        heating_end = t0 + timedelta(hours=1)
        history = [
            (t0 - timedelta(minutes=10), 48.0, 0.0),
            (t0, 46.6, 0.6),
            (heating_end, 41.0, 0.6),
            (
                heating_end + timedelta(minutes=2),
                55.0,
                0.0,
            ),  # history ends, never settles
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(rates.heating_sample_count, 0)

    def test_a_heating_segment_shorter_than_the_minimum_is_dropped(self):
        # nimbus issue #592's own caveat: "when the pump does not reach
        # its heating threshold the rate should not be learned from
        # that run." A 2-minute blip is well under the 6-minute floor.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (t0, 50.0, 0.6),
            (t0 + timedelta(minutes=2), 50.5, 0.6),
            (t0 + timedelta(hours=5), 49.0, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )

    def test_a_heating_segment_that_never_exceeds_the_threshold_is_ignored(self):
        # A compressor that never actually draws above on_threshold_kw
        # (a bridge reporting "on" with only standby draw) must never be
        # treated as a real heating segment at all.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (t0, 50.0, 0.02),
            (t0 + timedelta(hours=1), 50.2, 0.02),
            (t0 + timedelta(hours=6), 49.0, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )

    def test_single_sample_history_is_a_safe_default_no_op(self):
        rates = tf.learn_thermal_rates(
            [(datetime(2026, 9, 6, 6, 0, tzinfo=_TZ), 50.0, 0.0)],
            on_threshold_kw=0.05,
        )
        self.assertFalse(rates.is_learned)


class TestProjectTemperatureForecast(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 9, 9, 6, 0, tzinfo=_TZ)

    def _plan(self, values, hours=1.0):
        return [
            {"time": (self.t0 + timedelta(hours=hours * i)).isoformat(), "value": v}
            for i, v in enumerate(values)
        ]

    def test_empty_plan_returns_empty_forecast(self):
        self.assertEqual(
            tf.project_temperature_forecast(
                [],
                start_temperature=50.0,
                heating_rate_c_per_kwh=8.0,
                idle_decay_c_per_hour=0.5,
                on_threshold_kw=0.05,
            ),
            [],
        )

    def test_idle_period_decays_by_the_real_period_duration(self):
        plan = self._plan([0.0, 0.0], hours=2.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 2h at 0.5 degC/h decay -> -1.0 degC.
        self.assertAlmostEqual(forecast[0]["value"], 52.8)

    def test_heating_period_gains_rate_times_real_delivered_kwh(self):
        plan = self._plan([0.65, 0.65], hours=1.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 0.65kW * 1h = 0.65kWh * 8 degC/kWh = 5.2 degC gained.
        self.assertAlmostEqual(forecast[0]["value"], 59.0)

    def test_scenario_matches_592s_own_worked_example_shape(self):
        # nimbus issue #592's own worked example: idle from 06:00 to
        # 11:00 (53.8 -> ~52 degC), then heating from 11:00 -- crossing
        # 60 degC partway through, well before the run's full target.
        plan = (
            self._plan([0.0] * 5, hours=1.0)  # 06:00-11:00, idle
            + [
                {
                    "time": (self.t0 + timedelta(hours=5 + i)).isoformat(),
                    "value": 0.65,
                }
                for i in range(4)  # 11:00 onward, heating
            ]
        )
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 5h idle at 0.5 degC/h -> 53.8 - 2.5 = 51.3, matching the
        # issue's own "~52 degC by 11:00" (its own figure is rounded).
        self.assertAlmostEqual(forecast[4]["value"], 51.3)
        # Crosses 60 degC partway through the heating run, not at its
        # very end -- the whole point of #592's own worked example.
        crossing_idx = next(i for i, p in enumerate(forecast) if p["value"] >= 60.0)
        self.assertLess(crossing_idx, len(forecast) - 1)

    def test_ceiling_temperature_caps_a_heating_gain(self):
        # nimbus issue #610: "no ceiling: the projection keeps adding
        # heating_rate x kWh past the heater's own setpoint." A 65 degC
        # setpoint must never be exceeded by a heating step, however
        # large the nominal gain would otherwise be.
        plan = self._plan(
            [2.0, 2.0], hours=1.0
        )  # 2kW for 2h -> 32 degC of nominal gain
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=60.0,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
            ceiling_temperature=65.0,
        )
        self.assertLessEqual(forecast[0]["value"], 65.0)
        self.assertLessEqual(forecast[1]["value"], 65.0)
        self.assertEqual(forecast[1]["value"], 65.0)

    def test_ceiling_temperature_does_not_block_a_decay_step(self):
        # The ceiling only ever caps a HEATING gain -- decay must stay
        # free to fall below it, otherwise a load sitting idle above its
        # own setpoint (e.g. right after a real fix lands) could never
        # be shown cooling back down.
        # Two periods so the real 1h spacing applies to period 0 (a
        # single-period plan falls back to the documented 0.5h edge
        # case, not what this test means to exercise).
        plan = self._plan([0.0, 0.0], hours=1.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=66.0,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
            ceiling_temperature=65.0,
        )
        self.assertAlmostEqual(forecast[0]["value"], 65.5)

    def test_override_first_period_power_replaces_only_period_zero(self):
        # nimbus issue #611: the plan's own period 0 can genuinely
        # disagree with what the guard has ALREADY dispatched this cycle
        # (the hold window holding a real ON that this fresh solve's own
        # plan doesn't yet reflect). Only period 0 is overridden -- every
        # later period still projects from the real plan.
        plan = self._plan([0.0, 0.0], hours=1.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=50.0,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
            override_first_period_power_kw=0.65,
        )
        # Period 0 heats (override), period 1 decays (real plan, unchanged).
        self.assertGreater(forecast[0]["value"], 50.0)
        self.assertLess(forecast[1]["value"], forecast[0]["value"])


class TestFindFloorCrossing(unittest.TestCase):
    """nimbus issue #712/#713 (Mark Purcell, real live finding: two
    consecutive nights of uncontrolled compressor cut-in, both times the
    tank falling past its own hardware floor hours before the next
    scheduled ON period)."""

    def _series(self, values):
        t0 = datetime(2026, 9, 10, 16, 0, tzinfo=_TZ)
        return [
            {"time": (t0 + timedelta(hours=i)).isoformat(), "value": v}
            for i, v in enumerate(values)
        ]

    def test_no_crossing_returns_none(self):
        series = self._series([50.0, 49.0, 48.0, 47.0])
        self.assertIsNone(tf.find_floor_crossing(series, floor_temperature=45.0))

    def test_returns_the_first_period_that_crosses_not_a_later_one(self):
        # #713's own real shape: idle decay carries the tank below the
        # floor at 07:30, tomorrow's plan doesn't start heating again
        # until 08:00 -- the warning needs the FIRST crossing (07:30),
        # not to keep reporting every period it stays below the floor.
        series = self._series([50.0, 47.0, 44.0, 41.0, 39.96, 45.0, 52.0])
        crossing = tf.find_floor_crossing(series, floor_temperature=45.0)
        self.assertIsNotNone(crossing)
        self.assertEqual(crossing["value"], 44.0)
        self.assertEqual(crossing["time"], series[2]["time"])

    def test_exact_floor_value_counts_as_a_crossing(self):
        series = self._series([50.0, 45.0, 40.0])
        crossing = tf.find_floor_crossing(series, floor_temperature=45.0)
        self.assertIsNotNone(crossing)
        self.assertEqual(crossing["value"], 45.0)

    def test_empty_series_returns_none(self):
        self.assertIsNone(tf.find_floor_crossing([], floor_temperature=45.0))


class TestResolveFloorTemperature(unittest.TestCase):
    """nimbus issue #712/#713 -- the shared min_temp-then-done_when
    fallback, factored out so the pre-solve scheduling check and the
    post-solve warning never drift into two different answers."""

    def _parse_done_when(self, done_when):
        # A minimal stand-in for done_condition.parse_done_when -- this
        # module is deliberately import-free of done_condition (same
        # zero-project-import posture the rest of this file already
        # tests), so the real parser is injected by the real caller;
        # here we only need SOME callable matching its (op_fn, threshold)
        # return shape.
        threshold = float(done_when.lstrip(">=<! "))
        return (lambda a, b: a >= b), threshold

    def test_min_temp_wins_when_present(self):
        result = tf.resolve_floor_temperature(45.0, ">= 60", self._parse_done_when)
        self.assertEqual(result, 45.0)

    def test_falls_back_to_done_when_threshold_without_min_temp(self):
        result = tf.resolve_floor_temperature(None, ">= 60", self._parse_done_when)
        self.assertEqual(result, 60.0)

    def test_none_when_neither_available(self):
        self.assertIsNone(
            tf.resolve_floor_temperature(None, None, self._parse_done_when)
        )

    def test_malformed_min_temp_falls_back_to_done_when(self):
        result = tf.resolve_floor_temperature(
            "not-a-number", ">= 60", self._parse_done_when
        )
        self.assertEqual(result, 60.0)

    def test_malformed_done_when_with_no_min_temp_returns_none(self):
        def _raising_parser(_done_when):
            raise ValueError("bad done_when")

        self.assertIsNone(
            tf.resolve_floor_temperature(None, "garbage", _raising_parser)
        )


class TestNaiveFloorCrossingPeriod(unittest.TestCase):
    """nimbus issue #712/#713 -- the pre-solve, plan-free "if nothing
    heats this between now and whenever" projection that actually feeds
    build_controllable_loads()'s own scheduling decision."""

    def _grid(self, n=20, step_minutes=5):
        t0 = datetime(2026, 9, 10, 16, 0, tzinfo=_TZ)
        return [t0 + timedelta(minutes=step_minutes * i) for i in range(n)]

    def test_real_713_shape_crosses_within_the_grid(self):
        # #713's own real numbers: tank at 50 degC at 16:00, eco floor at
        # 45 degC, DEFAULT_IDLE_DECAY_C_PER_HOUR (0.5 degC/h) -- crosses
        # 10 hours later, well inside a 96h/5-min grid.
        grid = self._grid(n=200)
        now = grid[0]
        period = tf.naive_floor_crossing_period(
            grid, now, 50.0, tf.DEFAULT_IDLE_DECAY_C_PER_HOUR, 45.0
        )
        self.assertIsNotNone(period)
        crossing_time = now + timedelta(
            hours=(50.0 - 45.0) / tf.DEFAULT_IDLE_DECAY_C_PER_HOUR
        )
        self.assertGreaterEqual(grid[period], crossing_time)
        self.assertLess(grid[period - 1], crossing_time)

    def test_already_at_or_below_floor_returns_none(self):
        grid = self._grid()
        period = tf.naive_floor_crossing_period(grid, grid[0], 44.0, 0.5, 45.0)
        self.assertIsNone(period)

    def test_non_positive_decay_rate_returns_none(self):
        grid = self._grid()
        self.assertIsNone(
            tf.naive_floor_crossing_period(grid, grid[0], 50.0, 0.0, 45.0)
        )
        self.assertIsNone(
            tf.naive_floor_crossing_period(grid, grid[0], 50.0, -0.1, 45.0)
        )

    def test_missing_inputs_return_none(self):
        grid = self._grid()
        self.assertIsNone(
            tf.naive_floor_crossing_period(grid, grid[0], None, 0.5, 45.0)
        )
        self.assertIsNone(
            tf.naive_floor_crossing_period(grid, grid[0], 50.0, None, 45.0)
        )
        self.assertIsNone(
            tf.naive_floor_crossing_period(grid, grid[0], 50.0, 0.5, None)
        )

    def test_empty_grid_returns_none(self):
        self.assertIsNone(
            tf.naive_floor_crossing_period(
                [], datetime(2026, 9, 10, tzinfo=_TZ), 50.0, 0.5, 45.0
            )
        )

    def test_crossing_past_the_whole_horizon_returns_none(self):
        # A short grid and a very slow decay -- the crossing genuinely
        # falls after the last grid point, not actionable this cycle.
        grid = self._grid(n=4, step_minutes=5)
        period = tf.naive_floor_crossing_period(grid, grid[0], 50.0, 0.01, 45.0)
        self.assertIsNone(period)


if __name__ == "__main__":
    unittest.main()
