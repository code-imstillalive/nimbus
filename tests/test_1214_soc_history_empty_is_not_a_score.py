"""nimbus #1214: a configured battery SoC sensor that returns no history
must not be scored as though the battery were at half charge.

**The measured incident.** On 2026-09-24 the reference household's real
midnight SoC was **16.6%**. Production scored the day at **EPR 21.11%**
(`j_ach -3.05`, `regret $21.03`). A second install scoring the *same day*
from the *same mirrored sensors*, but with the real SoC, published
**68.86%** (`j_ach -14.62`, `regret $8.50`). The hourly `battery_kw`
series were identical to four decimal places on both boxes, so the entire
**47.75-point** difference came from one number: `initial_pct` falling
back to its hardcoded 50.0.

Starting 33 points too high also pushed the achieved trajectory through
the top of the pack -- that day reported `achieved_soc_max_pct 125.48`
and `achieved_above_ceiling_kwh 30.52`. A reconstruction above 100% SoC
is not a score needing a caveat, it is arithmetic about a battery that
does not exist.

**Why it is transient.** `fetch_entity_history_range()` waits
`future.result(timeout=30)` on a recorder read and degrades to `[]` on any
failure, logging only at DEBUG. The daily retrain saturates the executor
at 06:00: the rescore began at 06:00:03.517 and the first "previous cycle
still in progress" warning landed at 06:00:33.638 -- 30.1 s later, the
timeout expiring to the second. All 21 of that day's skip warnings fell in
hour 06 and none in the other 23, which is exactly why only the 06:00
rescore was ever corrupted.

So the fix refuses the day and lets the existing retry win it back, the
same posture the solar/load/battery emptiness check and #984's coverage
gate already take.

The properties pinned here:

1. **A configured sensor with no history returns None**, rather than a
   score built on an assumed battery.
2. **An install with NO SoC sensor configured is untouched** -- it has
   nothing to wait for, is not failing, and keeps the existing default.
   Refusing there would silently stop scoring every such install.
3. **A working read is unaffected**, and the real SoC is what gets used.
   A guard that also changes the healthy path is not this fix.
4. **The skip escalates** INFO -> WARNING, so a transient miss is quiet
   and a persistent one is loud.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

AEST = timezone(timedelta(hours=10))
DAY_START = datetime(2026, 9, 24, 0, 0, tzinfo=AEST)
DAY_END = DAY_START + timedelta(days=1)

SOC_SENSOR = "sensor.combined_soc"
REAL_SOC_PCT = 16.6  # the household's real midnight SoC on the incident day


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": SOC_SENSOR,
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


def _flat(value, start, end, step_minutes=15):
    out = []
    t = start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _prices(start, end, cheap, expensive, expensive_hour=17):
    out = []
    t = start
    while t < end:
        out.append((t, expensive if t.hour >= expensive_hour else cheap))
        t += timedelta(minutes=15)
    return out


def _make_fetch(soc_rows):
    """Every sensor healthy except the SoC one, which returns `soc_rows`."""

    def _fetch(entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat(2.0, start, end)
        if entity_id == "sensor.real_battery":
            return _flat(0.0, start, end)
        if entity_id == "sensor.import_price":
            return _prices(start, end, 0.05, 0.35)
        if entity_id == "sensor.export_price":
            return _prices(start, end, 0.02, 0.10)
        if entity_id == SOC_SENSOR:
            return soc_rows(start, end) if callable(soc_rows) else soc_rows
        return []

    return _fetch


def _report(cfg, soc_rows):
    with patch.object(
        solver_writer, "fetch_entity_history_range", side_effect=_make_fetch(soc_rows)
    ):
        return solver_writer._compute_report_for_window(
            cfg, DAY_START, DAY_END, allow_partial=True
        )


def _reset_counter():
    # getattr, so the control tests below can also run against a build
    # that does NOT have the fix. A control that cannot execute without
    # the change it is controlling for is not a control.
    getattr(solver_writer, "_SOC_HISTORY_SKIP_COUNTS", {}).clear()


class TestAConfiguredSensorWithNoHistoryIsRefused(unittest.TestCase):
    """Property 1 -- the incident itself."""

    def setUp(self):
        _reset_counter()

    def test_empty_soc_history_returns_none(self):
        self.assertIsNone(_report(_cfg(), []))

    def test_it_does_not_quietly_score_at_the_assumed_half_charge(self):
        """The whole defect: a real install WITH a SoC sensor being priced
        as though the pack were at 50%."""
        result = _report(_cfg(), [])
        self.assertIsNone(
            result,
            "a day whose opening SoC is unknown must not publish a score -- "
            "this is what produced EPR 21.11% against a true 68.86%",
        )


class TestAnInstallWithNoSocSensorIsUntouched(unittest.TestCase):
    """Property 2 -- the control that keeps this from breaking every
    install that never configured one."""

    def setUp(self):
        _reset_counter()

    def test_no_configured_sensor_still_scores(self):
        cfg = _cfg()
        del cfg["solver_battery_soc_sensor"]
        result = _report(cfg, [])
        self.assertIsNotNone(
            result,
            "an install with no SoC sensor is not failing and has nothing "
            "to retry -- refusing here would silently stop scoring it",
        )

    def test_an_empty_string_sensor_counts_as_unconfigured(self):
        result = _report(_cfg(solver_battery_soc_sensor=""), [])
        self.assertIsNotNone(result)


class TestAWorkingReadIsUnaffected(unittest.TestCase):
    """Property 3 -- the control. A guard that changes the healthy path is
    not this fix."""

    def setUp(self):
        _reset_counter()

    def test_real_soc_history_still_scores(self):
        result = _report(_cfg(), lambda s, e: _flat(REAL_SOC_PCT, s, e))
        self.assertIsNotNone(result)

    def test_the_real_soc_is_what_gets_used_not_the_default(self):
        """Pins that the guard did not accidentally replace a good read
        with the assumption it exists to prevent."""
        result = _report(_cfg(), lambda s, e: _flat(REAL_SOC_PCT, s, e))
        hourly = result["j_ach_hourly"]
        first = hourly[min(hourly)]
        # Battery is flat 0.0 kW in this fixture, so the opening SoC is
        # carried straight through rather than drifting.
        self.assertAlmostEqual(first["soc_pct"], REAL_SOC_PCT, places=1)
        self.assertNotAlmostEqual(
            first["soc_pct"], solver_writer._ASSUMED_INITIAL_SOC_PCT, places=1
        )


class TestTheSkipEscalates(unittest.TestCase):
    """Property 4 -- quiet on a transient miss, loud on a persistent one."""

    def setUp(self):
        _reset_counter()

    def test_first_attempts_are_info_then_it_warns(self):
        with self.assertLogs(solver_writer._LOGGER, level=logging.INFO) as cap:
            for _ in range(solver_writer._COVERAGE_SKIP_WARN_AFTER):
                _report(_cfg(), [])
        levels = [r.levelno for r in cap.records if "SoC sensor" in r.getMessage()]
        self.assertTrue(levels, "the skip must say something")
        self.assertEqual(levels[0], logging.INFO, "a first miss is routine")
        self.assertEqual(
            levels[-1], logging.WARNING, "a persistent miss is a real fault"
        )

    def test_the_message_names_the_sensor_and_the_day(self):
        with self.assertLogs(solver_writer._LOGGER, level=logging.INFO) as cap:
            _report(_cfg(), [])
        msg = next(
            m for m in (r.getMessage() for r in cap.records) if "SoC sensor" in m
        )
        self.assertIn(SOC_SENSOR, msg)
        self.assertIn("2026-09-24", msg)


class TestTheAssumedValueIsNamedNotRepeated(unittest.TestCase):
    def test_the_constant_exists_and_is_the_documented_midpoint(self):
        self.assertEqual(solver_writer._ASSUMED_INITIAL_SOC_PCT, 50.0)

    def test_no_bare_literal_remains_in_the_initial_pct_resolution(self):
        """Two uses that must not drift apart -- the resample default and
        the no-sensor fallback."""
        import inspect

        src = inspect.getsource(solver_writer._compute_report_for_window)
        start = src.index("initial_pct = (")
        block = src[start : start + 400]
        self.assertNotIn("50.0", block, "the magic number should be named here")
        self.assertEqual(block.count("_ASSUMED_INITIAL_SOC_PCT"), 2)


if __name__ == "__main__":
    unittest.main()
