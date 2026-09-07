"""Real regression tests for nimbus issues #542/#543 (Mark Purcell, real
household finding on this repo's own v0.94.168 install):

- #542 item 1: a solar-forecast entity configured directly as
  solver_solar_forecast_sensor_1/2/3 was silently dropped from every
  solve whenever it wasn't already shaped as the generic
  forecast=[{time,value,lower,upper}] array -- Solcast's own native
  entities publish detailedForecast=[...] instead, and Open-Meteo Solar
  Forecast's own entities publish watts={timestamp: value}. This writer
  already knew how to read both shapes (fetch_solcast_solar_raw()/
  fetch_open_meteo_solar_raw()), just not when a household pointed a
  *configured* source at them directly. _solar_entries_from_attributes()
  is the one shared reshape function closing that gap.
- #542 item 2: once item 1 lands, a household with the same entity BOTH
  explicitly configured AND auto-included would double-weight it in the
  blend's mean -- the configured_solar_entities/skip_entities threading
  through fetch_open_meteo_solar_raw()/fetch_solcast_solar_raw() closes
  that.
- #543: the "solar source ... dropped from this solve's blend" warning
  used to fire on every solve (205 copies in 4h on one real install) --
  now keyed once per (entity_id, reason), with a genuinely different
  reason ("unavailable" vs "shape not recognized" vs "malformed") each
  getting its own one-time log, and a recovery logged once at INFO.

_solar_entries_from_attributes()/_warn_solar_source_dropped_once()/
_note_solar_source_recovered() are module-level and pure/near-pure
beyond logging, so exercised directly (real functions, not a
reimplementation). fetch_solar_source_safe()/fetch_open_meteo_solar_raw()/
fetch_solcast_solar_raw() and the configured_solar_entities dedup wiring
are nested closures inside main() (well over 1000 lines, live ha_get/
ha_post_state calls throughout) -- too large to mock end-to-end for this
one fix, so their wiring is verified source-inspection style, matching
the existing precedent in tests/test_solver_writer_solar_fallback_not_
crash.py and tests/test_stale_devices_cleanup.py.
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
import solver_writer

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


class TestSolarEntriesFromAttributes(unittest.TestCase):
    def test_generic_forecast_shape_passes_through_unchanged(self):
        forecast = [{"time": "2026-09-08T00:00:00+10:00", "value": 1.5}]
        attrs = {"forecast": forecast}
        self.assertIs(solver_writer._solar_entries_from_attributes(attrs), forecast)

    def test_solcast_detailed_forecast_shape_is_reshaped(self):
        attrs = {
            "detailedForecast": [
                {
                    "period_start": "2026-09-08T06:00:00+10:00",
                    "pv_estimate": 2.1,
                    "pv_estimate10": 1.0,
                    "pv_estimate90": 3.0,
                }
            ]
        }
        result = solver_writer._solar_entries_from_attributes(attrs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["time"], "2026-09-08T06:00:00+10:00")
        self.assertEqual(result[0]["value"], 2.1)
        self.assertEqual(result[0]["lower"], 1.0)
        self.assertEqual(result[0]["upper"], 3.0)

    def test_solcast_missing_estimate_fields_default_to_zero_not_crash(self):
        attrs = {"detailedForecast": [{"period_start": "2026-09-08T06:00:00+10:00"}]}
        result = solver_writer._solar_entries_from_attributes(attrs)
        self.assertEqual(result[0]["value"], 0.0)
        self.assertEqual(result[0]["lower"], 0.0)
        self.assertEqual(result[0]["upper"], 0.0)

    def test_open_meteo_watts_shape_is_reshaped_and_scaled_to_kw(self):
        attrs = {"watts": {"2026-09-08T06:00:00+10:00": 2500.0}}
        result = solver_writer._solar_entries_from_attributes(attrs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["time"], "2026-09-08T06:00:00+10:00")
        self.assertAlmostEqual(result[0]["value"], 2.5)
        self.assertNotIn("lower", result[0])

    def test_forecast_shape_takes_priority_over_detailed_forecast(self):
        forecast = [{"time": "x", "value": 1.0}]
        attrs = {
            "forecast": forecast,
            "detailedForecast": [{"period_start": "y", "pv_estimate": 9.0}],
        }
        self.assertIs(solver_writer._solar_entries_from_attributes(attrs), forecast)

    def test_no_recognized_shape_returns_none(self):
        attrs = {"unit_of_measurement": "kW", "some_other_attribute": 42}
        self.assertIsNone(solver_writer._solar_entries_from_attributes(attrs))

    def test_empty_attributes_returns_none(self):
        self.assertIsNone(solver_writer._solar_entries_from_attributes({}))


class TestWarnSolarSourceDroppedOnce(unittest.TestCase):
    """nimbus issue #543: log once per (entity_id, reason), not every
    solve -- same #313/#314 discipline as _DONE_CONDITION_WARNED/
    _LOAD_POWER_SENSOR_UNIT_HINT_LOGGED, but with recovery logging on
    top (this project's own first log-once dedup to add that)."""

    def setUp(self):
        self._orig_warned = set(solver_writer._SOLAR_SOURCE_WARNED)
        solver_writer._SOLAR_SOURCE_WARNED.clear()

    def tearDown(self):
        solver_writer._SOLAR_SOURCE_WARNED.clear()
        solver_writer._SOLAR_SOURCE_WARNED.update(self._orig_warned)

    def test_first_call_warns(self):
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            solver_writer._warn_solar_source_dropped_once(
                "sensor.solcast_today", "unavailable", "URLError"
            )
        self.assertEqual(len(captured.records), 1)
        self.assertIn("sensor.solcast_today", captured.records[0].message)
        self.assertIn("unavailable", captured.records[0].message)

    def test_the_same_entity_and_reason_only_warns_once(self):
        solver_writer._warn_solar_source_dropped_once(
            "sensor.solcast_today", "unavailable", "URLError"
        )
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="WARNING"),
        ):
            solver_writer._warn_solar_source_dropped_once(
                "sensor.solcast_today", "unavailable", "URLError"
            )

    def test_a_different_reason_on_the_same_entity_warns_again(self):
        # #542/#543's own real shape: an entity that WAS unreachable
        # (network) and is now reachable but has an unrecognized shape
        # is a genuinely new, distinct diagnostic event.
        solver_writer._warn_solar_source_dropped_once(
            "sensor.solcast_today", "unavailable", "URLError"
        )
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured:
            solver_writer._warn_solar_source_dropped_once(
                "sensor.solcast_today", "shape not recognized", "no forecast attribute"
            )
        self.assertEqual(len(captured.records), 1)

    def test_repeat_of_the_same_condition_logs_debug_not_warning(self):
        solver_writer._warn_solar_source_dropped_once(
            "sensor.solcast_today", "unavailable", "URLError"
        )
        with self.assertLogs(solver_writer._LOGGER, level="DEBUG") as captured:
            solver_writer._warn_solar_source_dropped_once(
                "sensor.solcast_today", "unavailable", "URLError"
            )
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.DEBUG)

    def test_recovery_clears_the_entity_and_logs_info_once(self):
        solver_writer._warn_solar_source_dropped_once(
            "sensor.solcast_today", "unavailable", "URLError"
        )
        with self.assertLogs(solver_writer._LOGGER, level="INFO") as captured:
            solver_writer._note_solar_source_recovered("sensor.solcast_today")
        self.assertTrue(
            any(
                "contributing to the blend again" in r.message for r in captured.records
            )
        )
        # Cleared -- the same failure recurring later must warn again,
        # not stay silently suppressed by the old entry.
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as captured2:
            solver_writer._warn_solar_source_dropped_once(
                "sensor.solcast_today", "unavailable", "URLError"
            )
        self.assertEqual(len(captured2.records), 1)

    def test_recovery_on_a_never_warned_entity_is_a_silent_noop(self):
        with (
            self.assertRaises(AssertionError),
            self.assertLogs(solver_writer._LOGGER, level="INFO"),
        ):
            solver_writer._note_solar_source_recovered("sensor.never_failed")

    def test_recovery_clears_only_the_matching_entity(self):
        solver_writer._warn_solar_source_dropped_once(
            "sensor.a", "unavailable", "URLError"
        )
        solver_writer._warn_solar_source_dropped_once(
            "sensor.b", "unavailable", "URLError"
        )
        solver_writer._note_solar_source_recovered("sensor.a")
        self.assertIn(("sensor.b", "unavailable"), solver_writer._SOLAR_SOURCE_WARNED)
        self.assertNotIn(
            ("sensor.a", "unavailable"), solver_writer._SOLAR_SOURCE_WARNED
        )


def _extract_function_source(src: str, def_line: str, max_chars: int = 4000) -> str:
    start = src.index(def_line)
    return src[start : start + max_chars]


class TestSolarSourceWiringSourceInspection(unittest.TestCase):
    """fetch_solar_source_safe()/fetch_open_meteo_solar_raw()/
    fetch_solcast_solar_raw() and the configured_solar_entities dedup
    are nested closures inside main() -- see this file's own module
    docstring for why source-inspection is the right tool here, matching
    tests/test_solver_writer_solar_fallback_not_crash.py's own
    precedent."""

    @classmethod
    def setUpClass(cls):
        cls.src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")

    def test_fetch_solar_source_safe_uses_the_shared_reshape_helper(self):
        block = _extract_function_source(self.src, "    def fetch_solar_source_safe(")
        self.assertIn("_solar_entries_from_attributes(attrs)", block)

    def test_fetch_solar_source_safe_distinguishes_unavailable_from_shape(self):
        block = _extract_function_source(self.src, "    def fetch_solar_source_safe(")
        self.assertIn('"unavailable"', block)
        self.assertIn('"shape not recognized"', block)

    def test_fetch_solar_source_safe_reports_recovery_on_success(self):
        block = _extract_function_source(self.src, "    def fetch_solar_source_safe(")
        self.assertIn("_note_solar_source_recovered(entity_id)", block)

    def test_open_meteo_raw_accepts_and_honours_skip_entities(self):
        block = _extract_function_source(
            self.src, "    def fetch_open_meteo_solar_raw("
        )
        self.assertIn("skip_entities", block)
        self.assertIn("_solar_entries_from_attributes(", block)

    def test_solcast_raw_accepts_and_honours_skip_entities(self):
        block = _extract_function_source(self.src, "    def fetch_solcast_solar_raw(")
        self.assertIn("skip_entities", block)
        self.assertIn("_solar_entries_from_attributes(", block)

    def test_configured_solar_entities_is_computed_before_auto_include_and_passed_through(
        self,
    ):
        marker = "    configured_solar_entities = frozenset("
        self.assertIn(marker, self.src)
        start = self.src.index(marker)
        # The auto-include call site (fetcher(configured_solar_entities))
        # must appear AFTER configured_solar_entities is computed, and
        # actually pass it through -- not just define it unused.
        after = self.src[start:]
        self.assertIn("fetcher(configured_solar_entities)", after)


if __name__ == "__main__":
    unittest.main()
