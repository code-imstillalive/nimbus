"""nimbus issue #1784 (Mark Purcell): the solar "running low" gate.

When measured PV (a 30-minute mean of `solver_solar_power_sensor`, read from
the recorder) runs well below Solcast's p50, the rest of TODAY is planned on
Solcast's p10. Each rule Mark set out on the issue has a test here:

- the lagged reference: the lower of the current half-hour's p50 and the one
  30 minutes earlier, so a rising morning curve does not trip it;
- it judges only while both p50s are at least the minimum (2 kW);
- hysteresis: on below 70 %, held until above 90 %;
- only today's remaining periods switch to p10; tomorrow stays p50;
- inert without a Solcast p10, and off without recorder history;
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

    def __init__(self, today, tomorrow=None, history=None, live_w="500"):
        self.states = {SOLCAST_TODAY: {"detailedForecast": today}}
        if tomorrow is not None:
            self.states[SOLCAST_TOMORROW] = {"detailedForecast": tomorrow}
        self.history = history if history is not None else []
        self.live_w = live_w
        self.history_calls = 0

    def ha_get(self, entity_id):
        if entity_id == SENSOR:
            return {"state": self.live_w, "attributes": {"unit_of_measurement": "W"}}
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
    cfg = {
        "solver_auto_include_known_solar": True,
        "solver_solar_power_sensor": SENSOR,
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
            return srl.evaluate_gate(solver_writer, cfg or _cfg(), now, True)

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
        self.assertEqual(pub["periods_on_p10"], today_periods)

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

    def test_other_blend_members_are_not_touched(self):
        """Open-Meteo stays at its own forecast; only Solcast's member moves."""
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10, minute=3)
        grid = [now, TODAY.replace(hour=11)]
        fake = _Fake(_day(TODAY, p50, p10), history=_held(now, 500))
        fake.states["sensor.home_energy_production_today"] = {
            "watts": {TODAY.replace(hour=h).isoformat(): 6000.0 for h in range(24)}
        }
        (kw, _lo, _up), pub = self._build(fake, now, grid)
        self.assertTrue(pub["active"])
        self.assertAlmostEqual(kw[1], (6.0 + 1.5) / 2)


class TestInertAndOff(_Base):
    def test_without_a_solcast_p10_it_is_inert(self):
        p50, p10 = _flat_daylight(4.0, 1.5)
        now = TODAY.replace(hour=10)
        grid = [now, now + timedelta(hours=1)]
        fake = _Fake(_day(TODAY, p50, p10, with_p10=False), history=_held(now, 0))
        (kw, _lo, _up), pub = self._build(fake, now, grid)
        self.assertEqual(pub["reason"], "no_solcast_p10")
        self.assertFalse(pub["active"])
        self.assertAlmostEqual(kw[1], 4.0)
        self.assertEqual(fake.history_calls, 0)

    def test_without_solcast_in_the_blend_it_is_inert_and_says_so_once(self):
        now = TODAY.replace(hour=10)
        states = {
            "sensor.other": {
                "forecast": [{"time": now.isoformat(), "value": 4.0}],
            }
        }
        cfg = {
            "solver_solar_forecast_sensor": "sensor.other",
            "solver_solar_power_sensor": SENSOR,
        }
        with (
            patch.object(
                solver_writer,
                "ha_get",
                lambda e: {"attributes": states.get(e, {}), "state": "0"},
            ),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
            self.assertLogs(solver_shared._LOGGER, level="DEBUG") as logs,
        ):
            solar_inputs.build_solar_arrays(cfg, [now], 1, now)
            solar_inputs.build_solar_arrays(cfg, [now], 1, now)
        self.assertEqual(srl.published_result()["reason"], "no_solcast")
        inert = [m for m in logs.output if "#1784" in m and "inert" in m]
        self.assertEqual(len(inert), 1, logs.output)

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


class TestPublishedShape(_Base):
    def test_every_reason_publishes_the_same_keys(self):
        keys = set(srl._gate_result("not_evaluated"))
        for reason in ("off", "no_solcast", "no_history", "judged", "error"):
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
                "periods_on_p10",
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
