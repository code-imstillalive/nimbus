"""nimbus issue #1784 (Mark Purcell): the solar "running low" gate.

When measured PV (a 30-minute mean of `solver_solar_power_sensor`, read from
the recorder) runs well below the p50 forecast, the rest of TODAY is planned
on a lower forecast (Solcast's p10 where Solcast is there). Each rule Mark set out on the issue has a test here:

- the lagged reference: the lower of the current half-hour's p50 and the one
  30 minutes earlier, so a rising morning curve does not trip it;
- it judges only while both p50s are at least the minimum (2 kW);
- hysteresis: on below 70 %, held until above 90 %;
- only today's remaining periods switch to p10; tomorrow stays p50;
- Solcast p10 when Solcast is the reference; otherwise each member's own lower
  bound, or option (b) (ratio-scaled, floored at 0.3) when it has none;
- the blend itself is the reference when Solcast is absent; off without
  recorder history;
- 0 turns it off;
- and Mark's own 16 Oct 07:00 numbers (0.33 kW against 2.88 kW) turn it on.

Synthetic data only. Only the I/O boundary is patched (ha_get,
entity_exists, fetch_entity_history_range, _kw_scale_factor); the Solcast
reshape, resampling and time-weighted mean are the real functions.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_shared
import solver_writer
from solver_inputs import solar as solar_inputs
from solver_inputs import solar_running_low as srl

TZ = solver_writer.LOCAL_TZ
TODAY = datetime(2026, 10, 16, tzinfo=TZ)
TOMORROW = TODAY + timedelta(days=1)
SENSOR = "sensor.pv_power"
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
# nimbus #1994: what sensor.nimbus_solver_config publishes once the entity
# registry has found Solcast (integration + unique_id); the gate reads this,
# never an entity_id literal.
KNOWN = {"known_sources": {"solcast": [SOLCAST_TODAY, SOLCAST_TOMORROW]}}


def _day(day, p50_at, p10_at, with_p10=True):
    """A Solcast detailedForecast for one day: 48 half-hours."""
    out = []
    for k in range(48):
        start = day + timedelta(minutes=30 * k)
        row = {
            "period_start": start.isoformat(),
            "pv_estimate": p50_at(start),
            "pv_estimate90": p50_at(start) * 1.2,
        }
        if with_p10:
            row["pv_estimate10"] = p10_at(start)
        out.append(row)
    return out


def _flat_daylight(p50, p10):
    """p50/p10 constant 06:00-18:00, 0 outside."""

    def at(level):
        return lambda t: level if 6 <= t.hour < 18 else 0.0

    return at(p50), at(p10)


class _Fake:
    """A fake HA: Solcast today/tomorrow, a W PV sensor with a fixed
    history window, and a live PV reading for the index-0 anchor."""

    def __init__(
        self, today, tomorrow=None, history=None, live_w="500", live_changed=None
    ):
        self.states = {SOLCAST_TODAY: {"detailedForecast": today}}
        self.live_changed = live_changed
        if tomorrow is not None:
            self.states[SOLCAST_TOMORROW] = {"detailedForecast": tomorrow}
        self.history = history if history is not None else []
        self.live_w = live_w
        self.history_calls = 0

    def ha_get(self, entity_id):
        if entity_id == SENSOR:
            out = {"state": self.live_w, "attributes": {"unit_of_measurement": "W"}}
            if self.live_changed is not None:
                out["last_changed"] = self.live_changed.isoformat()
            return out
        return {"attributes": self.states[entity_id]}

    def entity_exists(self, entity_id):
        return entity_id in self.states or entity_id == SENSOR

    def fetch(self, entity_id, start, end):
        self.history_calls += 1
        assert entity_id == SENSOR
        return [(t, v) for t, v in self.history if t < end]

    def patches(self):
        return (
            patch.object(solver_writer, "ha_get", self.ha_get),
            patch.object(solver_writer, "entity_exists", self.entity_exists),
            patch.object(solver_writer, "fetch_entity_history_range", self.fetch),
            patch.object(solver_writer, "_kw_scale_factor", lambda e: 0.001),
        )


def _held(now, watts):
    """History holding `watts` all window (HA's start-time state)."""
    return [(now - timedelta(minutes=30), float(watts))]


def _cfg(**overrides):
    # The rule tests below test one rule each, so they run with the
    # confirmation window off (0 = on at the first low reading);
    # TestConfirmation tests the window, including its default.
    cfg = {
        "solver_auto_include_known_solar": True,
        "solver_solar_power_sensor": SENSOR,
        "solver_solar_running_low_confirm_minutes": 0,
        **KNOWN,
    }
    cfg.update(overrides)
    return cfg


class _Base(unittest.TestCase):
    def setUp(self):
        srl.reset_gate_state()
        solver_writer._SOLAR_SOURCE_WARNED.clear()

    def tearDown(self):
        srl.reset_gate_state()

    def _evaluate(self, fake, now, cfg=None):
        p = fake.patches()
        with p[0], p[1], p[2], p[3]:
            return srl.evaluate_gate(solver_writer, cfg or _cfg(), now, None)[0]

    def _build(self, fake, now, grid, cfg=None):
        p = fake.patches()
        with p[0], p[1], p[2], p[3]:
            arrays = solar_inputs.build_solar_arrays(
                cfg or _cfg(), grid, len(grid), now
            )
        return arrays, srl.published_result()


class TestTheLaggedReference(_Base):
    """A 30-minute mean trails a rising morning curve by about its own
    length. Against the current p50 alone it trips every dawn; against the
    lower of now and 30 minutes earlier it does not."""

    def _rising(self):
        # p50 rises 2.2 kW (06:30) -> 4.0 kW (07:00); p10 well below.
        p50 = {6.5: 2.2, 7.0: 4.0, 7.5: 5.5}

        def at(t):
            return p50.get(t.hour + t.minute / 60, 0.0 if t.hour < 6 else 6.0)

        return _day(TODAY, at, lambda t: at(t) * 0.5)

    def test_a_rising_morning_curve_does_not_trip_it(self):
        now = TODAY.replace(hour=7, minute=10)
        # Actual tracked the rising curve: its 30-minute mean is 2.0 kW,
        # 91 % of the 06:30 period's 2.2 kW but only 50 % of 07:00's 4.0.
        fake = _Fake(self._rising(), history=_held(now, 2000))
        result = self._evaluate(fake, now)
        self.assertEqual(result["reason"], "judged")
        self.assertEqual(result["p50_now_kw"], 4.0)
        self.assertEqual(result["p50_lagged_kw"], 2.2)
        self.assertEqual(result["reference_kw"], 2.2)
        self.assertFalse(result["active"])

    def test_the_lower_of_the_two_is_the_reference_on_a_falling_curve(self):
        # Afternoon: p50 falls 5.0 (14:00) -> 3.0 (14:30). The mean lags
        # the other way (reads high), so the lower value is the fair one.
        def at(t):
            return {14.0: 5.0, 14.5: 3.0}.get(t.hour + t.minute / 60, 4.0)

        now = TODAY.replace(hour=14, minute=40)
        fake = _Fake(_day(TODAY, at, lambda t: 1.0), history=_held(now, 2400))
        result = self._evaluate(fake, now)
        self.assertEqual(result["reference_kw"], 3.0)
        # 2.4 / 3.0 = 0.8: not low. Against the lagged 5.0 alone it would be
        # 0.48 and trip.
        self.assertFalse(result["active"])


class TestTheMinimum(_Base):
    def test_it_does_not_judge_while_either_p50_is_under_the_minimum(self):
        def at(t):
            return {6.5: 1.9, 7.0: 3.0}.get(t.hour + t.minute / 60, 0.0)

        now = TODAY.replace(hour=7, minute=5)
        fake = _Fake(_day(TODAY, at, lambda t: 0.5), history=_held(now, 0))
        result = self._evaluate(fake, now)
        self.assertEqual(result["reason"], "below_minimum")
        self.assertFalse(result["active"])
        # Nothing to judge, so the recorder is not even asked.
        self.assertEqual(fake.history_calls, 0)

    def test_the_minimum_is_a_setting(self):
        def at(t):
            return {6.5: 1.9, 7.0: 3.0}.get(t.hour + t.minute / 60, 0.0)

        now = TODAY.replace(hour=7, minute=5)
        fake = _Fake(_day(TODAY, at, lambda t: 0.5), history=_held(now, 0))
        result = self._evaluate(
            fake, now, _cfg(solver_solar_running_low_min_p50_kw=1.5)
        )
        self.assertEqual(result["reason"], "judged")
        self.assertTrue(result["active"])


class TestHysteresis(_Base):
    def test_on_below_70_held_until_above_90(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        steps = [
            # (minute after 10:00, actual W, expected active)
            (0, 2900, False),  # 72.5 %: not yet low
            (5, 2700, True),  # 67.5 %: on
            (10, 3400, True),  # 85 %: held on (below 90)
            (15, 3500, True),  # 87.5 %: still held
            (20, 3700, False),  # 92.5 %: off
            (25, 3000, False),  # 75 %: off stays off (not below 70)
            (30, 2600, True),  # 65 %: on again
        ]
        for minute, watts, expected in steps:
            now = TODAY.replace(hour=10, minute=minute)
            result = self._evaluate(_Fake(today, history=_held(now, watts)), now)
            self.assertEqual(result["active"], expected, (minute, watts, result))

    def test_the_thresholds_are_settings(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10)
        cfg = _cfg(
            solver_solar_running_low_on_percent=50,
            solver_solar_running_low_off_percent=60,
        )
        fake = _Fake(_day(TODAY, p50, p10), history=_held(now, 2200))  # 55 %
        self.assertFalse(self._evaluate(fake, now, cfg)["active"])
        fake = _Fake(_day(TODAY, p50, p10), history=_held(now, 1900))  # 47.5 %
        self.assertTrue(self._evaluate(fake, now, cfg)["active"])

    def test_since_is_when_it_turned_on_and_clears_when_off(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        on_at = TODAY.replace(hour=10)
        self._evaluate(_Fake(today, history=_held(on_at, 1000)), on_at)
        later = on_at + timedelta(minutes=5)
        result = self._evaluate(_Fake(today, history=_held(later, 1000)), later)
        self.assertEqual(result["on_since"], on_at.isoformat())
        done = later + timedelta(minutes=5)
        result = self._evaluate(_Fake(today, history=_held(done, 4000)), done)
        self.assertIsNone(result["on_since"])


class TestOnlyTodaysRemainingPeriods(_Base):
    def test_today_moves_to_p10_and_tomorrow_stays_p50(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10, minute=3)
        grid = [now] + [
            TODAY.replace(hour=10, minute=30) + timedelta(hours=k) for k in range(30)
        ]
        fake = _Fake(
            _day(TODAY, p50, p10),
            _day(TOMORROW, p50, p10),
            history=_held(now, 1000),  # 25 % of 4.0 kW
            live_w="1100",
        )
        (kw, lo, up), pub = self._build(fake, now, grid)
        self.assertTrue(pub["active"])
        # Index 0 is the live anchor, as before.
        self.assertAlmostEqual(kw[0], 1.1)
        for i, t in enumerate(grid[1:], start=1):
            expected = (
                (1.5 if t.date() == TODAY.date() else 4.0) if 6 <= t.hour < 18 else 0.0
            )
            self.assertAlmostEqual(kw[i], expected, msg=t.isoformat())
            self.assertLessEqual(lo[i], kw[i])
            self.assertGreaterEqual(up[i], kw[i])
        today_periods = sum(1 for t in grid[1:] if t.date() == TODAY.date())
        self.assertEqual(pub["periods_lowered"], today_periods)

    def test_while_off_the_plan_is_byte_identical(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10, minute=3)
        grid = [now + timedelta(minutes=30 * k) for k in range(60)]
        healthy = _Fake(
            _day(TODAY, p50, p10), _day(TOMORROW, p50, p10), history=_held(now, 3900)
        )
        on, pub_on = self._build(healthy, now, grid)
        self.assertFalse(pub_on["active"])
        srl.reset_gate_state()
        off, pub_off = self._build(
            healthy, now, grid, _cfg(solver_solar_running_low_min_p50_kw=0)
        )
        self.assertEqual(pub_off["reason"], "off")
        self.assertEqual(on, off)

    def _with_other_members(self, history_w):
        """Solcast (p50 4.0, p10 1.5), plus Open-Meteo at 6 kW (no lower
        bound) and a configured generic forecast at 5 kW with a lower
        bound of 3.5 kW -- the shape of the reference household's blend
        (Nimbus's own forecaster carries an 80 % band)."""
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10, minute=3)
        grid = [now, TODAY.replace(hour=11), TOMORROW.replace(hour=11)]
        fake = _Fake(
            _day(TODAY, p50, p10),
            _day(TOMORROW, p50, p10),
            history=_held(now, history_w),
        )
        hours = [TODAY + timedelta(hours=h) for h in range(48)]
        fake.states["sensor.home_energy_production_today"] = {
            "watts": {t.isoformat(): 6000.0 for t in hours}
        }
        fake.states["sensor.own"] = {
            "forecast": [
                {"time": t.isoformat(), "value": 5.0, "lower": 3.5, "upper": 6.5}
                for t in hours
            ]
        }
        known = {
            "solcast": [SOLCAST_TODAY, SOLCAST_TOMORROW],
            "open_meteo": ["sensor.home_energy_production_today"],
        }
        return (
            fake,
            now,
            grid,
            _cfg(solver_solar_forecast_sensor="sensor.own", known_sources=known),
        )

    def test_every_member_is_lowered_by_its_own_path(self):
        fake, now, grid, cfg = self._with_other_members(2000)  # ratio 0.5
        (kw, lo, _up), pub = self._build(fake, now, grid, cfg)
        self.assertTrue(pub["active"])
        self.assertEqual(pub["reference_source"], "solcast")
        self.assertEqual(
            (
                pub["members_p10"],
                pub["members_lower_bound"],
                pub["members_ratio_scaled"],
            ),
            (1, 1, 1),
        )
        self.assertEqual(pub["ratio_scale_used"], 0.5)
        # Today 11:00: own -> its lower 3.5, Open-Meteo -> 6 x 0.5, Solcast -> p10.
        self.assertAlmostEqual(kw[1], (3.5 + 3.0 + 1.5) / 3)
        self.assertLessEqual(lo[1], kw[1])
        # Tomorrow untouched.
        self.assertAlmostEqual(kw[2], (5.0 + 6.0 + 4.0) / 3)

    def test_the_ratio_scale_is_floored_at_point_three(self):
        fake, now, grid, cfg = self._with_other_members(400)  # ratio 0.1
        (kw, _lo, _up), pub = self._build(fake, now, grid, cfg)
        self.assertEqual(pub["ratio_scale_used"], 0.3)
        self.assertAlmostEqual(kw[1], (3.5 + 6.0 * 0.3 + 1.5) / 3)

    def test_solcast_without_p10_is_ratio_scaled_not_zeroed(self):
        """A missing pv_estimate10 is filled with 0.0 by the shared
        reshape; taking that as a lower bound would zero today's solar."""
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10, minute=3)
        grid = [now, TODAY.replace(hour=11)]
        fake = _Fake(_day(TODAY, p50, p10, with_p10=False), history=_held(now, 2000))
        (kw, lo, _up), pub = self._build(fake, now, grid)
        self.assertTrue(pub["active"])
        self.assertEqual(pub["members_ratio_scaled"], 1)
        self.assertAlmostEqual(kw[1], 2.0)
        self.assertLessEqual(lo[1], kw[1])


def _const(v):
    return lambda t: v


class TestWithoutSolcastTheBlendIsTheReference(_Base):
    """No Solcast on the install: the reference is the blended p50 itself,
    remembered in process; the lagged value is the record 30-40 min ago."""

    def _fake(self, value, history_w, now):
        hours = [TODAY + timedelta(hours=h) for h in range(48)]
        fake = _Fake([], history=_held(now, history_w))
        fake.states = {
            "sensor.own": {
                "forecast": [
                    {
                        "time": t.isoformat(),
                        "value": value(t),
                        "lower": 0.6 * value(t),
                    }
                    for t in hours
                ]
            }
        }
        return fake

    def _own_cfg(self):
        return {
            "solver_solar_forecast_sensor": "sensor.own",
            "solver_solar_power_sensor": SENSOR,
            "solver_solar_running_low_confirm_minutes": 0,
        }

    def test_first_solve_cannot_judge_then_judges_from_memory(self):
        first = TODAY.replace(hour=10)
        _arrays, pub = self._build(
            self._fake(_const(4.0), 1000, first),
            first,
            [first, first + timedelta(hours=1)],
            self._own_cfg(),
        )
        self.assertEqual(pub["reason"], "no_reference")
        self.assertEqual(pub["reference_source"], "blend")
        self.assertFalse(pub["active"])
        later = first + timedelta(minutes=30)
        (kw, _lo, _up), pub = self._build(
            self._fake(_const(4.0), 1000, later),
            later,
            [later, later + timedelta(hours=1)],
            self._own_cfg(),
        )
        self.assertEqual(pub["reason"], "judged")
        self.assertTrue(pub["active"])
        self.assertEqual(pub["members_lower_bound"], 1)
        self.assertAlmostEqual(kw[1], 2.4)  # its own lower bound, 0.6 x 4.0

    def test_the_lag_rule_holds_on_the_blend_too(self):
        """Rising morning: blend 2.2 kW 30 min ago, 4.0 now, actual 2.0 --
        91 % of the lagged value, so off (50 % of now would trip it)."""
        first = TODAY.replace(hour=7)
        switch = TODAY.replace(hour=7, minute=30)

        def rising(t):
            return 2.2 if t < switch else 4.0

        self._build(self._fake(rising, 0, first), first, [first], self._own_cfg())
        later = first + timedelta(minutes=30)
        _arrays, pub = self._build(
            self._fake(rising, 2000, later), later, [later], self._own_cfg()
        )
        self.assertEqual(pub["reason"], "judged")
        self.assertEqual(pub["reference_kw"], 2.2)
        self.assertFalse(pub["active"])

    def test_a_record_older_than_40_minutes_is_not_the_lag(self):
        first = TODAY.replace(hour=10)
        self._build(
            self._fake(_const(4.0), 1000, first), first, [first], self._own_cfg()
        )
        later = first + timedelta(minutes=45)
        _arrays, pub = self._build(
            self._fake(_const(4.0), 1000, later), later, [later], self._own_cfg()
        )
        self.assertEqual(pub["reason"], "no_reference")


class TestInertAndOff(_Base):
    def test_without_any_forecast_there_is_no_reference(self):
        now = TODAY.replace(hour=10)
        cfg = {"solver_solar_power_sensor": SENSOR, **KNOWN}
        with (
            patch.object(
                solver_writer, "ha_get", lambda e: {"attributes": {}, "state": "0"}
            ),
            patch.object(solver_writer, "entity_exists", lambda e: False),
        ):
            solar_inputs.build_solar_arrays(cfg, [now], 1, now)
        self.assertEqual(srl.published_result()["reason"], "no_reference")
        self.assertFalse(srl.published_result()["active"])

    def test_no_history_means_off_even_if_it_was_on(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        now = TODAY.replace(hour=10)
        self.assertTrue(
            self._evaluate(_Fake(today, history=_held(now, 500)), now)["active"]
        )
        later = now + timedelta(minutes=5)
        result = self._evaluate(_Fake(today, history=[]), later)
        self.assertEqual(result["reason"], "no_history")
        self.assertFalse(result["active"])

    def test_a_history_read_that_raises_is_off_not_a_crash(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10)
        fake = _Fake(_day(TODAY, p50, p10))

        def boom(*_a):
            raise RuntimeError("recorder down")

        fake.fetch = boom
        self.assertEqual(self._evaluate(fake, now)["reason"], "no_history")

    def test_zero_minimum_turns_it_off(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10)
        fake = _Fake(_day(TODAY, p50, p10), history=_held(now, 0))
        result = self._evaluate(fake, now, _cfg(solver_solar_running_low_min_p50_kw=0))
        self.assertEqual(result["reason"], "off")
        self.assertFalse(result["active"])
        self.assertEqual(fake.history_calls, 0)

    def test_without_a_pv_sensor_it_is_inert(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10)
        fake = _Fake(_day(TODAY, p50, p10), history=_held(now, 0))
        result = self._evaluate(fake, now, _cfg(solver_solar_power_sensor=None))
        self.assertEqual(result["reason"], "no_solar_power_sensor")


class TestMarks16October(_Base):
    """16 Oct 2026, Mark's install: at 07:00:00 the flag went on with a
    30-minute average of 0.33 kW against a reference of 2.88 kW, the first
    moment it was allowed to judge. Actual PV 06:30-07:00 was 0.15-0.6 kW.

    The 06:30 period's p50 (2.88 kW) is the reference; the 07:00 period's
    p50 is not stated on the issue and only has to be the higher of the two,
    so 3.40 kW stands in. The 06:00 period's p50 is under 2 kW, which is why
    06:59 cannot judge."""

    def _today(self):
        p50 = {6.0: 1.60, 6.5: 2.88, 7.0: 3.40}

        def at(t):
            return p50.get(t.hour + t.minute / 60, 3.4 if 7 <= t.hour < 17 else 0.0)

        return _day(TODAY, at, lambda t: at(t) * 0.8)

    def _history(self, now):
        # 150 W held 06:30-06:45, 510 W 06:45-07:00: a time-weighted 330 W.
        return [
            (TODAY.replace(hour=6, minute=30), 150.0),
            (TODAY.replace(hour=6, minute=45), 510.0),
        ]

    def test_0659_cannot_judge_and_0700_turns_it_on(self):
        before = TODAY.replace(hour=6, minute=59)
        result = self._evaluate(
            _Fake(self._today(), history=self._history(before)), before
        )
        self.assertEqual(result["reason"], "below_minimum")
        self.assertFalse(result["active"])

        at = TODAY.replace(hour=7)
        with self.assertLogs(solver_shared._LOGGER, level="WARNING") as logs:
            result = self._evaluate(_Fake(self._today(), history=self._history(at)), at)
        self.assertTrue(result["active"])
        self.assertAlmostEqual(result["actual_30min_avg_kw"], 0.33, places=3)
        self.assertEqual(result["reference_kw"], 2.88)
        self.assertEqual(result["p50_lagged_kw"], 2.88)
        self.assertEqual(result["p50_now_kw"], 3.4)
        self.assertAlmostEqual(result["ratio"], 0.33 / 2.88, places=4)
        self.assertEqual(result["on_since"], at.isoformat())
        self.assertTrue(
            any("running LOW" in m and "0.33" in m and "2.88" in m for m in logs.output)
        )


class TestConfirmation(_Base):
    """The gate turns on only once every judged reading for the confirmation
    window (default 10 min) has stayed below the on threshold. Measured on
    the reference household, 16 Oct 2026: a late start would have tripped
    the bare gate at 07:30 (ratio 0.61) and released it at 07:47; the ratio
    crossed 0.70 at 07:38, so a 10-minute window never trips."""

    def _run(self, readings, cfg_extra=None):
        """`readings`: (minute after 07:00, ratio) against a flat 4 kW p50.
        Returns the `active` flag after each reading."""
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        cfg = {
            "solver_auto_include_known_solar": True,
            "solver_solar_power_sensor": SENSOR,
            **KNOWN,
            **(cfg_extra or {}),
        }
        out = []
        for minute, ratio in readings:
            now = TODAY.replace(hour=7) + timedelta(minutes=minute)
            fake = _Fake(today, history=_held(now, ratio * 4000))
            out.append(self._evaluate(fake, now, cfg)["active"])
        return out

    def test_the_16_october_late_start_does_not_trip_it(self):
        """This morning's minute-by-minute ratios, 07:30-07:47."""
        ratios = [0.610, 0.621, 0.632, 0.644, 0.656, 0.669, 0.680, 0.694]
        ratios += [0.711, 0.729, 0.749, 0.771, 0.794, 0.814, 0.837, 0.859]
        ratios += [0.881, 0.903]
        readings = [(30 + i, r) for i, r in enumerate(ratios)]
        self.assertEqual(self._run(readings), [False] * len(readings))
        # The same morning trips the bare rule at 07:30 (the old behaviour).
        bare = self._run(readings, {"solver_solar_running_low_confirm_minutes": 0})
        self.assertTrue(bare[0])

    def test_a_sustained_low_trips_at_ten_minutes(self):
        readings = [(0, 0.55), (5, 0.57), (9, 0.56), (10, 0.58)]
        self.assertEqual(self._run(readings), [False, False, False, True])

    def test_a_low_that_recovers_inside_the_window_does_not_trip(self):
        readings = [(0, 0.55), (5, 0.75), (10, 0.58), (15, 0.6)]
        # 0.75 at 5 min ends the window; a new one starts at 10 min.
        self.assertEqual(self._run(readings), [False, False, False, False])

    def test_zero_is_immediate(self):
        readings = [(0, 0.55)]
        self.assertEqual(
            self._run(readings, {"solver_solar_running_low_confirm_minutes": 0}),
            [True],
        )

    def test_pending_since_is_published_and_cleared(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        cfg = {"solver_solar_power_sensor": SENSOR, **KNOWN}
        t0 = TODAY.replace(hour=10)
        result = self._evaluate(_Fake(today, history=_held(t0, 2000)), t0, cfg)
        self.assertEqual(result["pending_since"], t0.isoformat())
        self.assertFalse(result["active"])
        t1 = t0 + timedelta(minutes=10)
        result = self._evaluate(_Fake(today, history=_held(t1, 2000)), t1, cfg)
        self.assertTrue(result["active"])
        self.assertIsNone(result["pending_since"])

    def test_release_stays_immediate(self):
        readings = [(0, 0.5), (10, 0.5), (11, 0.95)]
        self.assertEqual(self._run(readings), [False, True, False])


class TestLogsOncePerChange(_Base):
    def test_one_warning_on_and_one_info_off(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                if "#1784" in record.getMessage():
                    records.append(record)

        handler = _H(level=logging.DEBUG)
        solver_shared._LOGGER.addHandler(handler)
        old = solver_shared._LOGGER.level
        solver_shared._LOGGER.setLevel(logging.DEBUG)
        try:
            for minute, watts in (
                (0, 500),
                (5, 500),
                (10, 500),
                (15, 3900),
                (20, 3900),
            ):
                now = TODAY.replace(hour=10, minute=minute)
                self._evaluate(_Fake(today, history=_held(now, watts)), now)
        finally:
            solver_shared._LOGGER.removeHandler(handler)
            solver_shared._LOGGER.setLevel(old)
        levels = [r.levelname for r in records]
        self.assertEqual(levels, ["WARNING", "INFO"], [r.getMessage() for r in records])
        self.assertIn("OFF", records[1].getMessage())


class TestRestart(_Base):
    def test_after_a_restart_it_starts_off_and_rejudges_from_history(self):
        """No warm-up: the recorder already holds the last 30 minutes, so
        the first solve after a restart judges straight away."""
        p50, p10 = _flat_daylight(4.0, 1.5)
        today = _day(TODAY, p50, p10)
        now = TODAY.replace(hour=12)
        self._evaluate(_Fake(today, history=_held(now, 500)), now)
        srl.reset_gate_state()  # a restart
        self.assertFalse(srl._STATE.active)
        later = now + timedelta(minutes=1)
        result = self._evaluate(_Fake(today, history=_held(later, 500)), later)
        self.assertTrue(result["active"])
        self.assertEqual(result["on_since"], later.isoformat())


class TestTheRecorderBehind(_Base):
    """A backup locks the recorder's database: new states queue in memory and
    the history read returns the last state written before the lock, held to
    now. The 30-minute mean then freezes on a morning value while the panels
    ramp up. The gate must not judge a frozen mean (20 Oct 2026, #1784)."""

    def _today(self):
        p50, p10 = _flat_daylight(5.0, 2.0)
        return _day(TODAY, p50, p10)

    def test_a_locked_recorder_turns_it_off_even_if_it_was_on(self):
        now = TODAY.replace(hour=8, minute=30)
        # Earlier: a genuinely low morning turned it on.
        early = now - timedelta(hours=1)
        self.assertTrue(
            self._evaluate(_Fake(self._today(), history=_held(early, 1000)), early)[
                "active"
            ]
        )
        # Now: the only row is the state carried in at the window start
        # (1.0 kW, written before the lock); live is 8 kW, changed seconds ago.
        fake = _Fake(
            self._today(),
            history=_held(now, 1000),
            live_w="8000",
            live_changed=now - timedelta(seconds=10),
        )
        result = self._evaluate(fake, now)
        self.assertEqual(result["reason"], "recorder_behind")
        self.assertFalse(result["active"])
        self.assertIsNone(result["actual_30min_avg_kw"])

    def test_without_the_live_comparison_the_same_history_would_hold_it_on(self):
        # The control: the frozen 1.0 kW mean against a 5 kW reference is
        # ratio 0.2, so a gate that trusts it stays on all morning.
        now = TODAY.replace(hour=8, minute=30)
        result = self._evaluate(_Fake(self._today(), history=_held(now, 1000)), now)
        self.assertEqual(result["reason"], "judged")
        self.assertTrue(result["active"])

    def test_a_sensor_that_held_one_value_is_not_behind(self):
        now = TODAY.replace(hour=10)
        fake = _Fake(
            self._today(),
            history=_held(now, 4800),
            live_w="4800",
            live_changed=now - timedelta(hours=2),
        )
        result = self._evaluate(fake, now)
        self.assertEqual(result["reason"], "judged")
        self.assertAlmostEqual(result["actual_30min_avg_kw"], 4.8, places=3)

    def test_an_ordinary_commit_delay_is_not_behind(self):
        now = TODAY.replace(hour=10)
        history = _held(now, 4800) + [(now - timedelta(minutes=3), 4900.0)]
        fake = _Fake(
            self._today(),
            history=history,
            live_w="5000",
            live_changed=now - timedelta(seconds=5),
        )
        self.assertEqual(self._evaluate(fake, now)["reason"], "judged")

    def test_the_threshold_is_five_minutes(self):
        self.assertEqual(srl.RECORDER_BEHIND_AFTER, timedelta(minutes=5))
        hist = [(TODAY.replace(hour=10), 1.0)]
        on_edge = _Fake(self._today(), live_changed=TODAY.replace(hour=10, minute=5))
        past_edge = _Fake(
            self._today(), live_changed=TODAY.replace(hour=10, minute=5, second=1)
        )
        self.assertIsNone(srl.recorder_behind_by(on_edge, SENSOR, hist))
        self.assertEqual(
            srl.recorder_behind_by(past_edge, SENSOR, hist),
            timedelta(minutes=5, seconds=1),
        )

    def test_an_unreadable_live_state_is_not_behind(self):
        hist = [(TODAY.replace(hour=10), 1.0)]

        class _Boom:
            def ha_get(self, _e):
                raise RuntimeError("gone")

        self.assertIsNone(srl.recorder_behind_by(_Boom(), SENSOR, hist))
        self.assertIsNone(srl.recorder_behind_by(_Fake(self._today()), SENSOR, hist))


class TestPublishedShape(_Base):
    def test_every_reason_publishes_the_same_keys(self):
        keys = set(srl._gate_result("not_evaluated"))
        for reason in (
            "off",
            "no_reference",
            "no_history",
            "recorder_behind",
            "judged",
            "error",
        ):
            self.assertEqual(set(srl._gate_result(reason)), keys)
        self.assertEqual(
            keys,
            {
                "active",
                "reason",
                "actual_30min_avg_kw",
                "p50_now_kw",
                "p50_lagged_kw",
                "reference_kw",
                "ratio",
                "on_since",
                "pending_since",
                "reference_source",
                "periods_lowered",
                "members_p10",
                "members_lower_bound",
                "members_ratio_scaled",
                "ratio_scale_used",
            },
        )

    def test_the_settings_defaults_match_const(self):
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "nimbus_load"
            / "const.py"
        ).read_text(encoding="utf-8")
        values = {}
        for node in ast.parse(src).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                try:
                    values[node.target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        self.assertEqual(
            values["CONF_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW"], srl.CFG_MIN_P50_KW
        )
        self.assertEqual(
            values["CONF_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT"], srl.CFG_ON_PERCENT
        )
        self.assertEqual(
            values["CONF_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT"], srl.CFG_OFF_PERCENT
        )
        self.assertEqual(
            values["DEFAULT_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW"],
            srl.DEFAULT_MIN_P50_KW,
        )
        self.assertEqual(
            values["DEFAULT_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT"],
            srl.DEFAULT_ON_PERCENT,
        )
        self.assertEqual(
            values["DEFAULT_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT"],
            srl.DEFAULT_OFF_PERCENT,
        )


if __name__ == "__main__":
    unittest.main()
