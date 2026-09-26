"""nimbus issue #1181: the home battery's achieved-SoC reconstruction had
no gap detection at all, and the guard that exists for this was applied
only to battery participants.

`_stale_power_period_indices()` shipped for #1161 and had exactly **one**
call site: `_resolve_battery_participant_history()`. Every install has a
home battery; most have no participants. So the guard protected nothing on
a typical install, and the one power series that always exists was
unmeasured.

Measured on the reference household's `solver_battery_power_sensor` over
the 2026-09-20 charge window (06:00-17:00 local): 640 points in 11.0 h,
median gap 30.2 s, largest gaps **1777 s (30 min)** and 1456 s, and
**5.93 h -- 54.0% of the window -- inside gaps longer than two minutes**.
`resample_history_mean()` holds the last observed value across each gap
(correctly, per #1008), so more than half the charge phase was integrated
from a reading last seen up to half an hour earlier, with nothing saying
so.

## Why this ships the DETECTION and not the ADJUSTMENT

The obvious fix is wrong, which is the whole reason #1181 was filed as an
issue rather than a PR. Zeroing stale periods -- what the participant path
does -- is conservative for **throughput**: crediting nothing for
unobserved time understates energy moved, the safe direction for
`achieved_energy_in/out_kwh`.

For a **trajectory** it is not conservative at all. The home
reconstruction already under-rises through the charge phase; zeroing 54%
of that window would deepen the under-rise and *increase*
`soc_discrepancy` -- making a household's EPR look **less** reliable
because of a fix. The same detection wants opposite treatments from its
two consumers.

So `_power_history_coverage()` measures and publishes, and
`actual_net_kw` is left exactly as it was. What that buys is the ability
to distinguish two situations `soc_discrepancy` alone cannot:

    "the reconstruction disagrees with the real sensor"
    "the reconstruction had almost nothing to work with"

Both read as `soc_discrepancy_reason: disagreement` today.

Option 3 in the issue -- refuse the day above a coverage threshold, in the
shape of #984's existing coverage gate -- is the issue's own inclination
and remains the likely endpoint. It is deliberately NOT done here: a
threshold cannot be chosen responsibly while the quantity it would gate on
is published nowhere. This is that quantity.

## What these tests pin

* The two fractions measure different things and the difference is the
  point. `time_in_long_gaps_pct` is threshold-free by construction (a gap
  longer than one grid period guarantees a period with no sample of its
  own); `stale_periods_pct` is the same permissive one-hour measure the
  participant path acts on. A 30-minute gap on a 15-minute grid is
  invisible to the second and obvious to the first.
* Honest absence, not a fabricated 0.0 -- "no data" must not read as
  "perfect coverage", which is the failure mode this whole issue is about.
* Gap time is clamped to the window. A sample hours before the window
  still matters (it is what the first period holds forward from) but the
  time before the window is not part of the window's own coverage.
* The measurement does not alter the numbers it sits next to.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

DAY = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
PERIOD_H = 0.25


def _grid(n_periods: int, period_hours: float = PERIOD_H) -> list[datetime]:
    return [DAY + timedelta(hours=period_hours * i) for i in range(n_periods)]


def _dense(n_periods: int, step_s: int = 30, value: float = 5.0):
    """A sample every `step_s` seconds across the whole window."""
    span = timedelta(hours=PERIOD_H * n_periods)
    out, t = [], DAY
    while t <= DAY + span:
        out.append((t, value))
        t += timedelta(seconds=step_s)
    return out


class TestHonestAbsence(unittest.TestCase):
    """A fabricated 0.0 would read as perfect coverage, which is exactly
    the misreading #1181 exists to prevent."""

    def test_no_grid_returns_none(self):
        self.assertIsNone(
            solver_writer._power_history_coverage(_dense(4), [], PERIOD_H)
        )

    def test_no_samples_returns_none(self):
        self.assertIsNone(solver_writer._power_history_coverage([], _grid(4), PERIOD_H))

    def test_a_single_sample_returns_none_because_a_gap_is_undefined(self):
        self.assertIsNone(
            solver_writer._power_history_coverage([(DAY, 1.0)], _grid(4), PERIOD_H)
        )


class TestFullCoverageReadsClean(unittest.TestCase):
    def test_a_dense_series_reports_no_long_gaps(self):
        out = solver_writer._power_history_coverage(_dense(48), _grid(48), PERIOD_H)
        assert out is not None
        self.assertEqual(out["time_in_long_gaps_pct"], 0.0)
        self.assertEqual(out["stale_periods_pct"], 0.0)
        self.assertAlmostEqual(float(out["median_gap_s"]), 30.0, places=1)
        self.assertAlmostEqual(float(out["max_gap_s"]), 30.0, places=1)


class TestTheTwoFractionsMeasureDifferentThings(unittest.TestCase):
    """The central claim. A gap big enough to hold power forward across
    several periods is INVISIBLE to the one-hour guard the participant
    path acts on -- which is why the participant-style measure alone would
    have reported this install as fine."""

    def test_a_thirty_minute_gap_is_invisible_to_the_one_hour_guard(self):
        # 8 hours of 15-minute periods, dense samples except one 30-minute
        # hole in the middle.
        n = 32
        hole_start = DAY + timedelta(hours=4)
        hole_end = hole_start + timedelta(minutes=30)
        pts = [(t, v) for t, v in _dense(n) if not (hole_start < t < hole_end)]
        out = solver_writer._power_history_coverage(pts, _grid(n), PERIOD_H)
        assert out is not None

        self.assertGreater(
            float(out["time_in_long_gaps_pct"]),
            5.0,
            "a 30-minute hole in an 8-hour window is ~6% of it and must "
            "show up in the threshold-free measure",
        )
        self.assertEqual(
            out["stale_periods_pct"],
            0.0,
            "the one-hour guard cannot see a 30-minute gap -- that is the "
            "asymmetry #1181 is about, and if this ever becomes non-zero "
            "the guard's own constant has changed",
        )
        self.assertGreaterEqual(float(out["max_gap_s"]), 1800.0)

    def test_a_gap_beyond_the_one_hour_guard_shows_in_both(self):
        n = 32
        hole_start = DAY + timedelta(hours=2)
        hole_end = hole_start + timedelta(hours=3)
        pts = [(t, v) for t, v in _dense(n) if not (hole_start < t < hole_end)]
        out = solver_writer._power_history_coverage(pts, _grid(n), PERIOD_H)
        assert out is not None
        self.assertGreater(float(out["time_in_long_gaps_pct"]), 30.0)
        self.assertGreater(
            float(out["stale_periods_pct"]),
            0.0,
            "a three-hour gap exceeds the one-hour guard and must be "
            "visible to the participant-style measure too",
        )


class TestGapTimeIsClampedToTheWindow(unittest.TestCase):
    """A sample from hours earlier still matters -- it is what the first
    periods hold forward FROM -- but the time before the window is not
    part of the window's own coverage, and counting it would let
    `time_in_long_gaps_pct` exceed 100."""

    def test_a_long_pre_window_gap_does_not_inflate_the_fraction(self):
        n = 8  # a 2-hour window
        pts = [(DAY - timedelta(hours=20), 5.0), *_dense(n)]
        out = solver_writer._power_history_coverage(pts, _grid(n), PERIOD_H)
        assert out is not None
        self.assertLessEqual(float(out["time_in_long_gaps_pct"]), 100.0)
        self.assertEqual(
            out["time_in_long_gaps_pct"],
            0.0,
            "the 20-hour gap lies entirely before the window, so none of "
            "it is this window's missing coverage",
        )
        self.assertGreaterEqual(
            float(out["max_gap_s"]),
            20 * 3600.0,
            "max_gap_s is a property of the history, not of the window, "
            "and should still report the real 20-hour gap",
        )


class TestItIsPublishedNextToTheDiscrepancy(unittest.TestCase):
    """Option 1's actual deliverable: the number reaches a consumer, on
    every return path, so a reader never has to handle the key being
    absent on only some of them."""

    def test_the_coverage_reaches_the_published_dict(self):
        coverage = {"points": 10, "time_in_long_gaps_pct": 54.0}
        hist = [(DAY + timedelta(minutes=15 * i), 50.0) for i in range(8)]
        rows = {
            (DAY + timedelta(hours=h)).isoformat(): {"soc_pct": 50.0} for h in range(2)
        }
        out = solver_writer._soc_discrepancy_stats(
            [(hist, 100.0)], rows, power_coverage=coverage
        )
        self.assertEqual(out["soc_discrepancy_power_coverage"], coverage)

    def test_it_is_present_on_the_honest_absence_path_too(self):
        coverage = {"points": 3}
        out = solver_writer._soc_discrepancy_stats([], {}, power_coverage=coverage)
        self.assertIsNone(out["soc_discrepancy_max_pct"])
        self.assertEqual(
            out["soc_discrepancy_power_coverage"],
            coverage,
            "coverage is a property of the history, not of whether the "
            "discrepancy could be computed -- and 'no SoC sensor' is "
            "exactly when a reader wants to know what history existed",
        )

    def test_omitting_it_changes_nothing_else(self):
        """Measurement only. The same inputs must give the same
        discrepancy numbers with and without the coverage argument."""
        hist = [(DAY + timedelta(minutes=15 * i), 40.0 + i) for i in range(12)]
        rows = {
            (DAY + timedelta(hours=h)).isoformat(): {"soc_pct": 45.0} for h in range(3)
        }
        without = solver_writer._soc_discrepancy_stats([(hist, 100.0)], rows)
        with_it = solver_writer._soc_discrepancy_stats(
            [(hist, 100.0)], rows, power_coverage={"points": 12}
        )
        for key in (
            "soc_discrepancy_max_pct",
            "soc_discrepancy_mean_pct",
            "soc_discrepancy_reliable",
            "soc_discrepancy_reason",
        ):
            with self.subTest(key=key):
                self.assertEqual(without[key], with_it[key])


class TestTheHomePathActuallyMeasuresItself(unittest.TestCase):
    """The asymmetry this issue is named for: `_power_history_coverage()`
    must be called on the HOME battery's own history, not only wired up
    for participants the way `_stale_power_period_indices()` was."""

    @staticmethod
    def _called_names() -> set[str]:
        """Function names actually CALLED inside
        `_compute_report_for_window`, via AST.

        Parsed rather than substring-matched because the home path's own
        comment names `_stale_power_period_indices()` while explaining why
        it is deliberately NOT called there -- a substring check reads that
        prose as a call and fails on the very comment documenting the
        decision. (It did, on the first run of this file.)
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(solver_writer._compute_report_for_window))
        )
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_the_home_reconstruction_measures_its_own_coverage(self):
        self.assertIn("_power_history_coverage", self._called_names())

    def test_the_home_reconstruction_is_not_adjusted(self):
        """The thing #1181 explicitly rules out. If the home path ever
        starts zeroing stale periods the way the participant path does,
        `soc_discrepancy` gets worse, not better -- so that change must be
        deliberate and must update this test and the issue's reasoning."""
        self.assertNotIn("_stale_power_period_indices", self._called_names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
