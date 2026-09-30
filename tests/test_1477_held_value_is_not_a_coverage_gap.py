"""nimbus issue #1477: a fully-closed day was refused 200+ times because the
coverage gate measured "last recorded row - first recorded row", and HA's
recorder writes a row only when a value CHANGES. A PV sensor holding 0.0
from dusk (last row 17:39:23 on the real install) to midnight therefore read
as a 17.66 h window.

Pinned: a series that stops early WITHOUT passing through a non-numeric state
is a held value and counts to the window end; one that went `unavailable` is
a real gap and is still refused; an unreadable recorder is still refused.
Uses the #313/#314 harness from test_quality_report_skip_logging.py.
"""

import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from test_quality_report_skip_logging import DAY_END, DAY_START, _cfg, _full_fetch

quality = sys.modules[solver_writer._compute_report_for_window.__module__]
LOGGER_NAME = solver_writer._LOGGER.name
DUSK = DAY_START.replace(hour=17, minute=39, second=23)


def _solar_stops_at_dusk(entity_id, start, end):
    pts = _full_fetch(entity_id, start, end)
    if entity_id == "sensor.real_solar":
        return [p for p in pts if p[0] <= DUSK]
    return pts


class TestAHeldValueIsNotAGap(unittest.TestCase):
    def setUp(self):
        solver_writer._COVERAGE_SKIP_COUNTS.clear()

    def _score(self, went_unavailable):
        calls = []

        def _probe(entity_id, after, end):
            calls.append((entity_id, after, end))
            return went_unavailable

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_solar_stops_at_dusk,
            ),
            patch.object(
                quality.solver_shared, "fetch_entity_went_unavailable", _probe
            ),
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        return result, calls

    def test_the_1477_shape_scores_when_the_sensor_merely_held_its_value(self):
        result, calls = self._score(went_unavailable=False)
        self.assertIsNotNone(result, "a quiet evening must not block the day")
        self.assertEqual([c[0] for c in calls], ["sensor.real_solar"])
        self.assertLessEqual(calls[0][1], DUSK + timedelta(minutes=15))

    def test_a_real_outage_after_the_last_row_is_still_refused(self):
        with self.assertLogs(LOGGER_NAME, level="INFO") as cm:
            result, _ = self._score(went_unavailable=True)
        self.assertIsNone(result)
        self.assertTrue(any("skip #1" in line for line in cm.output), cm.output)

    def test_an_unreadable_recorder_is_still_refused(self):
        result, _ = self._score(went_unavailable=None)
        self.assertIsNone(result, "unknown is not evidence the sensor was fine")

    def test_full_coverage_never_pays_for_the_extra_read(self):
        calls = []
        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_full_fetch
            ),
            patch.object(
                quality.solver_shared,
                "fetch_entity_went_unavailable",
                lambda *a: calls.append(a) or False,
            ),
        ):
            solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertEqual(calls, [])


class _Resp:
    def __init__(self, payload):
        import json as _json

        self._b = _json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestTheProbeItself(unittest.TestCase):
    """REST path of fetch_entity_went_unavailable (the cron/standalone shape;
    the native branch reads the same states from the recorder)."""

    AFTER = DUSK

    def _probe(self, rows=None, error=None):
        shared = quality.solver_shared
        sw = shared._solver_writer()
        payload = [
            [{"state": s, "last_changed": w.isoformat()} for s, w in (rows or [])]
        ]
        with (
            patch.object(sw.NATIVE, "hass", None),
            patch.object(sw, "_load_token", lambda: "t"),
            patch.object(
                shared.urllib.request,
                "urlopen",
                side_effect=error or (lambda *a, **k: _Resp(payload)),
            ),
        ):
            return shared.fetch_entity_went_unavailable(
                "sensor.pv", self.AFTER, DAY_END
            )

    def test_an_outage_after_the_last_row_is_reported(self):
        self.assertIs(self._probe([("unavailable", DUSK + timedelta(hours=2))]), True)

    def test_only_numbers_means_the_value_was_held(self):
        self.assertIs(self._probe([("0.0", DUSK + timedelta(hours=1))]), False)
        self.assertIs(self._probe([]), False)

    def test_an_outage_before_the_window_does_not_count(self):
        self.assertIs(self._probe([("unavailable", DUSK - timedelta(hours=1))]), False)

    def test_a_failed_read_is_unknown_not_fine(self):
        err = quality.solver_shared.urllib.error.URLError("down")
        self.assertIsNone(self._probe(error=err))


if __name__ == "__main__":
    unittest.main()
