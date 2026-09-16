"""nimbus issue #984: a non-empty history is not a covering one.

Found from a real install's own recorded history. Every morning at
~06:02, for at least three consecutive days, the daily rescore published:

    j_ref 2.3440   j_ach -0.5687   j_star -9.9092   ->  regret +9.34, EPR 23.77%

and ~5 minutes later the same scored day settled to:

    j_ref 4.7892   j_ach -15.8974  j_star -15.1667  ->  regret -0.73, EPR 103.66%

Every figure in the first set is a fraction of the second. That is a
PARTIAL window being scored as though it were a full day, not a
different day and not a different sensor.

`_compute_report_for_window()` could not catch it. Its `allow_partial`
guard compares `day_end - day_start` -- the window REQUESTED, always
exactly 24 h on the daily path -- and its only check on the fetched data
was emptiness, which a single row satisfies.

**Why this mattered enough to fix rather than note.** The wrong value is
not a transient on a dashboard: it is written to recorder history and to
long-term statistics, so any chart that aggregates a day by max, first
or last keeps picking it up permanently. A household reading "regret
$9.34" on three consecutive days was reading a recorder artefact, not
their dispatch -- and two charts of the same entity disagreed because
they happened to aggregate differently.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

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


class TestCoverageMeasurement(unittest.TestCase):
    def test_a_full_day_measures_as_a_full_day(self):
        full = _series(0.0, 24.0)
        self.assertAlmostEqual(
            solver_writer._history_coverage_hours((full, full, full), _START, _END),
            24.0,
            places=2,
        )

    def test_the_worst_series_decides(self):
        """The report is only as trustworthy as its thinnest input, so
        one truncated sensor must fail the day rather than be averaged
        away by two healthy ones."""
        full = _series(0.0, 24.0)
        truncated = _series(18.0, 24.0)  # only the last 6 h came back
        self.assertAlmostEqual(
            solver_writer._history_coverage_hours(
                (full, full, truncated), _START, _END
            ),
            6.0,
            places=2,
        )

    def test_an_empty_series_is_zero_not_an_error(self):
        full = _series(0.0, 24.0)
        self.assertEqual(
            solver_writer._history_coverage_hours((full, [], full), _START, _END), 0.0
        )

    def test_samples_outside_the_window_are_clipped_not_counted(self):
        """A fetch that overshoots the window must not be able to report
        MORE coverage than the window contains, or the guard could be
        passed by data belonging to another day."""
        overshooting = [
            (_START - timedelta(hours=5), 1.0),
            (_END + timedelta(hours=5), 1.0),
        ]
        self.assertAlmostEqual(
            solver_writer._history_coverage_hours(
                (overshooting, overshooting, overshooting), _START, _END
            ),
            24.0,
            places=2,
        )

    def test_a_single_row_reports_no_span(self):
        """The exact case the old emptiness check let through."""
        one = [(_START + timedelta(hours=6), 1.0)]
        self.assertEqual(
            solver_writer._history_coverage_hours((one, one, one), _START, _END), 0.0
        )


class TestTheThresholdSeparatesRealCasesFromTheDefect(unittest.TestCase):
    """The threshold has to admit an ordinary day with a gap and reject
    the half-empty window that caused #984. These pin both sides."""

    def _passes(self, covered_hours: float) -> bool:
        return covered_hours >= 24.0 * solver_writer._MIN_DAILY_COVERAGE_FRACTION

    def test_a_day_with_a_twenty_minute_sensor_dropout_still_scores(self):
        """Real installs drop samples -- a sensor goes unavailable, an
        integration reloads. Refusing those would mean never scoring."""
        self.assertTrue(self._passes(24.0 - 20.0 / 60.0))

    def test_a_day_with_a_two_hour_gap_still_scores(self):
        self.assertTrue(self._passes(22.0))

    def test_the_observed_half_empty_window_is_rejected(self):
        """The real failure produced figures roughly half the true ones,
        consistent with about half the window being returned."""
        self.assertFalse(self._passes(12.0))

    def test_a_six_hour_tail_is_rejected(self):
        self.assertFalse(self._passes(6.0))

    def test_the_threshold_is_not_so_tight_it_demands_perfection(self):
        """A threshold of 1.0 would reject any day missing a single
        sample at either edge, which is most real days."""
        self.assertLess(solver_writer._MIN_DAILY_COVERAGE_FRACTION, 1.0)
        self.assertGreater(solver_writer._MIN_DAILY_COVERAGE_FRACTION, 0.5)


if __name__ == "__main__":
    unittest.main()
