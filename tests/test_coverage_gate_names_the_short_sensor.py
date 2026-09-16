"""nimbus issue #1054 (Mark Purcell, real production install): the
coverage gate has to name WHICH sensor was short.

#984's gate correctly refused to publish 16 Sep as a full-day score --
real recorder history for the worst of solar/battery/load spanned only
17.37 h of the 24 h window. But the skip line reported nothing except
that minimum, so the issue's own stated next step became:

    "pull each of the three sensors' raw history for 2026-09-16 with
     real pagination ... and find the actual gap boundary"

a manual, paginated recorder pull to recover a per-sensor number
`_history_coverage_hours()` had already computed internally and thrown
away. These tests pin that the breakdown survives to the log line.

The second half is the level. INFO was right for what #984 expected --
the recorder still catching up just after midnight, which clears on the
next cycle. It was wrong for what happened: the same day skipped across
5+ consecutive solves with `sensor.nimbus_solver_quality_report` at
`unknown`, and nothing at the default log level saying why. The reason
was only recoverable by raising the logger to INFO by hand and re-running
`solve_now`.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from test_quality_report_skip_logging import (
    DAY_END,
    DAY_START,
    LOGGER_NAME,
    _cfg,
    _flat_history,
    _price_history,
)

_START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
_END = _START + timedelta(hours=24)


def _series(first_hour: float, last_hour: float, step_minutes: int = 5):
    """A history spanning [first_hour, last_hour] inside the window."""
    out = []
    t = _START + timedelta(hours=first_hour)
    stop = _START + timedelta(hours=last_hour)
    while t <= stop:
        out.append((t, 1.0))
        t += timedelta(minutes=step_minutes)
    return out


class TestPerSeriesCoverage(unittest.TestCase):
    """The breakdown itself."""

    def test_each_series_reports_its_own_span(self):
        got = solver_writer._history_coverage_by_series(
            {
                "solar": _series(0.0, 24.0),
                "battery": _series(0.0, 17.0),
                "load": _series(6.0, 24.0),
            },
            _START,
            _END,
        )
        self.assertAlmostEqual(got["solar"].hours, 24.0)
        self.assertAlmostEqual(got["battery"].hours, 17.0)
        self.assertAlmostEqual(got["load"].hours, 18.0)

    def test_an_empty_series_is_zero_without_erasing_the_others(self):
        """The distinction that makes an absent sensor diagnosable: a
        sensor that returned NO history reads 0.00 h, while its healthy
        neighbours still report their real spans. `_history_coverage_
        hours()` collapsed all three to a single 0.0."""
        got = solver_writer._history_coverage_by_series(
            {"solar": _series(0.0, 24.0), "battery": [], "load": _series(0.0, 24.0)},
            _START,
            _END,
        )
        self.assertEqual(got["battery"].hours, 0.0)
        self.assertIsNone(got["battery"].first)
        self.assertIsNone(got["battery"].last)
        self.assertAlmostEqual(got["solar"].hours, 24.0)
        self.assertAlmostEqual(got["load"].hours, 24.0)

    def test_a_single_row_reports_no_span(self):
        one = [(_START + timedelta(hours=3), 1.0)]
        got = solver_writer._history_coverage_by_series({"solar": one}, _START, _END)
        self.assertEqual(got["solar"].hours, 0.0)

    def test_samples_outside_the_window_are_clipped(self):
        wide = [
            (_START - timedelta(hours=5), 1.0),
            (_END + timedelta(hours=5), 1.0),
        ]
        got = solver_writer._history_coverage_by_series({"solar": wide}, _START, _END)
        self.assertAlmostEqual(got["solar"].hours, 24.0)
        self.assertEqual(got["solar"].first, _START)
        self.assertEqual(got["solar"].last, _END)


class TestDelegationIsBehaviourPreserving(unittest.TestCase):
    """`_history_coverage_hours()` now delegates here. Its own tests in
    test_quality_report_coverage_guard.py still pass unchanged; these
    pin the equivalence directly, including the two edges where the old
    short-circuit could have differed."""

    def _both(self, *histories):
        old = solver_writer._history_coverage_hours(histories, _START, _END)
        new = solver_writer._history_coverage_by_series(
            {str(i): h for i, h in enumerate(histories)}, _START, _END
        )
        return old, new

    def test_the_minimum_matches_on_a_normal_mix(self):
        old, new = self._both(
            _series(0.0, 24.0), _series(0.0, 17.0), _series(6.0, 24.0)
        )
        self.assertAlmostEqual(old, min(c.hours for c in new.values()))
        self.assertAlmostEqual(old, 17.0)

    def test_an_empty_series_still_collapses_the_minimum_to_zero(self):
        """The old code returned 0.0 for the WHOLE tuple the moment any
        series was empty. The minimum over the new dict has to agree --
        this is the threshold-behaviour guarantee."""
        old, new = self._both(_series(0.0, 24.0), [], _series(0.0, 24.0))
        self.assertEqual(old, 0.0)
        self.assertEqual(min(c.hours for c in new.values()), 0.0)

    def test_no_series_at_all_is_zero_not_a_valueerror(self):
        """min() of an empty dict raises; the guard for that is why the
        delegation is not a bare min() call."""
        self.assertEqual(solver_writer._history_coverage_hours((), _START, _END), 0.0)


def _truncated_battery_fetch(entity_id, start, end):
    """Mark's real 16 Sep shape: solar and load cover the full day, the
    battery sensor's raw history stops short."""
    if entity_id == "sensor.real_battery":
        return _flat_history(0.0, start, start + timedelta(hours=17, minutes=1))
    if entity_id == "sensor.real_solar":
        return _flat_history(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat_history(2.0, start, end)
    if entity_id == "sensor.import_price":
        return _price_history(start, end)
    if entity_id == "sensor.export_price":
        return _price_history(start, end, cheap=0.02, expensive=0.10)
    return []


class TestSkipLineNamesTheShortSensor(unittest.TestCase):
    def setUp(self):
        solver_writer._COVERAGE_SKIP_COUNTS.clear()
        self.addCleanup(solver_writer._COVERAGE_SKIP_COUNTS.clear)

    def _skip_once(self):
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_truncated_battery_fetch,
            ),
            self.assertLogs(LOGGER_NAME, level="INFO") as cm,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNone(result)
        return cm

    def test_the_short_sensors_entity_id_and_hours_are_in_the_line(self):
        """The whole point of #1054: no manual recorder pull needed to
        learn which of the three it was."""
        cm = self._skip_once()
        line = next(x for x in cm.output if "Nimbus quality: skip" in x)
        self.assertIn("sensor.real_battery 17.00 h", line)

    def test_the_healthy_sensors_are_named_too(self):
        """Naming only the loser would leave "is 17.37 h actually low
        for this install?" unanswerable. The comparison is the signal --
        and note the healthy pair read 23.75 h, not 24.00: a span
        measured first-sample-to-last-sample is always short by one
        sampling interval on a perfect day. That is exactly why the
        threshold is 0.9 rather than 1.0, and why an absolute figure
        alone is harder to read than three side by side."""
        line = next(x for x in self._skip_once().output if "Nimbus quality: skip" in x)
        self.assertIn("sensor.real_solar 23.75 h", line)
        self.assertIn("sensor.real_load 23.75 h", line)

    def test_the_short_sensor_is_listed_first(self):
        line = next(x for x in self._skip_once().output if "Nimbus quality: skip" in x)
        self.assertLess(
            line.index("sensor.real_battery"), line.index("sensor.real_solar")
        )

    def test_the_covered_span_boundary_is_in_the_line(self):
        """#1054's stated next step was a manual paginated recorder pull
        to "find the actual gap boundary". For a truncated window the
        covered span's own edges ARE that boundary, and the gate already
        has them -- so the line carries them and the manual pull is not
        needed to answer it."""
        line = next(x for x in self._skip_once().output if "Nimbus quality: skip" in x)
        self.assertIn("sensor.real_battery 17.00 h [00:00-17:00]", line)

    def test_an_entirely_empty_sensor_is_named_by_the_earlier_guard(self):
        """Worth pinning because it corrected a wrong assumption while
        writing this: the coverage gate's own `[no history]` branch is
        NOT how an absent sensor gets named end-to-end. #314's row-count
        guard sits upstream and catches an empty series first, already
        naming it per sensor. The two guards together mean a household
        is never told "something was short" without being told which --
        whether the sensor returned nothing or merely stopped early."""

        def _no_battery(entity_id, start, end):
            if entity_id == "sensor.real_battery":
                return []
            return _truncated_battery_fetch(entity_id, start, end)

        with (
            patch.object(
                solver_writer, "fetch_entity_history_range", side_effect=_no_battery
            ),
            self.assertLogs(LOGGER_NAME, level="INFO") as cm,
        ):
            result = solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        self.assertIsNone(result)
        line = next(x for x in cm.output if "Real history missing" in x)
        self.assertIn("battery=0 rows", line)

    def test_the_headline_minimum_is_unchanged(self):
        """#984's own number still leads the line -- this adds the
        breakdown, it does not replace the figure the gate acted on."""
        line = next(x for x in self._skip_once().output if "Nimbus quality: skip" in x)
        self.assertIn("only 17.00 h of the 24.00 h window", line)


class TestPersistentSkipEscalates(unittest.TestCase):
    def setUp(self):
        solver_writer._COVERAGE_SKIP_COUNTS.clear()
        self.addCleanup(solver_writer._COVERAGE_SKIP_COUNTS.clear)

    def _levels_over(self, n_cycles):
        levels = []
        for _ in range(n_cycles):
            with (
                patch.object(
                    solver_writer,
                    "fetch_entity_history_range",
                    side_effect=_truncated_battery_fetch,
                ),
                self.assertLogs(LOGGER_NAME, level="INFO") as cm,
            ):
                solver_writer._compute_report_for_window(
                    _cfg(), DAY_START, DAY_END, allow_partial=False
                )
            levels.append(
                next(
                    r.levelname
                    for r in cm.records
                    if "Nimbus quality: skip" in r.getMessage()
                )
            )
        return levels

    def test_the_first_retries_stay_routine(self):
        """A recorder catching up just after midnight must not page
        anyone -- that is the case #984 was built for."""
        self.assertEqual(
            self._levels_over(solver_writer._COVERAGE_SKIP_WARN_AFTER - 1),
            ["INFO"] * (solver_writer._COVERAGE_SKIP_WARN_AFTER - 1),
        )

    def test_a_day_that_keeps_failing_becomes_a_warning(self):
        """The real production symptom: 5+ consecutive skips, the sensor
        stuck at `unknown`, and nothing visible at default log level."""
        levels = self._levels_over(solver_writer._COVERAGE_SKIP_WARN_AFTER + 2)
        self.assertEqual(levels[-1], "WARNING")
        self.assertEqual(levels[solver_writer._COVERAGE_SKIP_WARN_AFTER - 1], "WARNING")

    def test_the_attempt_number_is_reported(self):
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_truncated_battery_fetch,
            ),
            self.assertLogs(LOGGER_NAME, level="INFO") as cm,
        ):
            solver_writer._compute_report_for_window(
                _cfg(), DAY_START, DAY_END, allow_partial=False
            )
        line = next(x for x in cm.output if "Nimbus quality: skip" in x)
        self.assertIn("skip #1 for 2026-08-24", line)

    def test_counting_is_per_day_not_global(self):
        """Two different thin days must each get their own grace period
        rather than the second inheriting the first's count."""
        self._levels_over(solver_writer._COVERAGE_SKIP_WARN_AFTER)
        other_start = DAY_START - timedelta(days=1)
        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=_truncated_battery_fetch,
            ),
            self.assertLogs(LOGGER_NAME, level="INFO") as cm,
        ):
            solver_writer._compute_report_for_window(
                _cfg(),
                other_start,
                other_start + timedelta(days=1),
                allow_partial=False,
            )
        level = next(
            r.levelname for r in cm.records if "Nimbus quality: skip" in r.getMessage()
        )
        self.assertEqual(level, "INFO")


if __name__ == "__main__":
    unittest.main()
