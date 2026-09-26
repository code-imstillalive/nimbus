"""nimbus issue #1228: the SoC discrepancy statistic was comparing an
hourly MEAN against a point sample, and that artifact alone was the cause
of `epr_reliable: False` on an install whose sensors were fine.

`_soc_discrepancy_stats()` differences two numbers per hour:

    real_pct  resample_history_nearest(hist, [hour_dt])  -> the real
              sensor's value AT `HH:00:00`. A point sample.
    ach_pct   j_ach_hourly[key]["soc_pct"]               -> that hour's
              MEAN of the achieved trajectory.

Both carry the same timestamp key, which is what that function's own
docstring means by "genuinely the same real hour on every side" -- a
claim about ALIGNMENT that was read for a long time as though it settled
COMPARABILITY too. It does not. Differencing a mean against an instant
injects roughly half the hour's ramp rate as pure artifact, largest
exactly where SoC moves fastest.

Measured on the reference household, 24 Sep 2026: a published
`max 21.17 / mean 9.88` whose max landed on **hour 12, the steepest
charge ramp of the day**, against a like-for-like `max 11.82 / mean 6.46`
that passes both of the configured thresholds.

The fix point-samples the ACHIEVED side rather than averaging the real
side. Both would make the comparison like-for-like; only one agrees with
`resample_history_mean()`'s own documented principle that SoC is a STATE,
sampled and held, not a flow to be averaged -- and point-sampling the
achieved side is also the smaller change, leaving the real side exactly
as it is.

`j_ach_hourly["soc_pct"]` is deliberately left as a mean: it feeds
Lovelace/apexcharts rows where an hourly mean is the right thing to plot,
and #356 commits that a <=24h window produces byte-identical output.

What these tests pin:

* The boundary sample is the END of the period BEFORE the hour starts,
  and the INITIAL SoC for hour 0 -- because the per-period array is
  `initial + cumsum(delta)`, i.e. end-of-period. Off by one period here
  would reintroduce a slice of the same artifact in the other direction.
* Its keys match `_hourly_means_by_key()`'s keys exactly. A mismatch
  would make the whole fix silently inert: the consumer would find
  nothing and fall back to the mean.
* On a ramp, mean and boundary genuinely differ by about half the ramp --
  the artifact, quantified, rather than asserted.
* A pure ramp with a PERFECTLY tracking real sensor reports a real gap
  before the fix and ~zero after. This is the issue, reproduced.
* The fallback is honest: no mapping -> the mean is used AND
  `soc_discrepancy_basis` says so, rather than quietly reverting.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Imported through the PACKAGE, not via `tests/_solver_path`. That helper
# puts `custom_components/nimbus_load` itself on sys.path, where the
# integration's own `select.py` (its HA select platform) SHADOWS the
# stdlib `select` module -- so the next `import socket`/`asyncio` anywhere
# fails with a partially-initialised-module ImportError. That is the cause
# of the large block of import-time failures this repo's test suite shows
# on a plain local run, and it bites any file that needs both
# `_solver_path` and Home Assistant.
from custom_components.nimbus_load import solver_writer
from custom_components.nimbus_load.solver.quality_report import (
    _hourly_means_by_key,
    _soc_pct_at_hour_boundaries,
)

BRISBANE = ZoneInfo("Australia/Brisbane")
DAY = datetime(2026, 9, 24, 0, 0, 0, tzinfo=BRISBANE)


def _quarter_hour_grid(n_hours: int = 24):
    return np.full(n_hours * 4, 0.25)


class TestBoundarySampleIsTheRightPeriod(unittest.TestCase):
    """The off-by-one that would quietly half-fix the issue."""

    def test_hour_zero_is_the_initial_soc(self):
        """Nothing has elapsed at the instant hour 0 begins, so the only
        correct value is the initial SoC -- not period 0's end, which
        already includes a quarter-hour of dispatch."""
        hours = _quarter_hour_grid(2)
        soc = np.arange(len(hours), dtype=float) + 100.0  # 100, 101, ...
        out = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=42.0, day_start=DAY
        )
        self.assertEqual(out[DAY.isoformat()], 42.0)

    def test_later_hours_take_the_end_of_the_previous_period(self):
        """The per-period array is `initial + cumsum(delta)`, so element
        t is the SoC AFTER period t. Hour 1 begins at period 4, so its
        boundary value is period 3's end."""
        hours = _quarter_hour_grid(3)
        soc = np.arange(len(hours), dtype=float)  # 0..11
        out = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=-1.0, day_start=DAY
        )
        self.assertEqual(out[(DAY + timedelta(hours=1)).isoformat()], 3.0)
        self.assertEqual(out[(DAY + timedelta(hours=2)).isoformat()], 7.0)

    def test_an_hourly_grid_works_too(self):
        """A 1.0-hour period grid is a real configuration, not only the
        15-minute one this household runs."""
        hours = np.full(3, 1.0)
        soc = np.array([10.0, 20.0, 30.0])
        out = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=5.0, day_start=DAY
        )
        self.assertEqual(out[DAY.isoformat()], 5.0)
        self.assertEqual(out[(DAY + timedelta(hours=1)).isoformat()], 10.0)
        self.assertEqual(out[(DAY + timedelta(hours=2)).isoformat()], 20.0)

    def test_an_empty_grid_returns_nothing_rather_than_raising(self):
        self.assertEqual(
            _soc_pct_at_hour_boundaries(
                hours=np.asarray([], dtype=float),
                soc_pct=np.asarray([], dtype=float),
                initial_soc_pct=1.0,
                day_start=DAY,
            ),
            {},
        )


class TestKeysMatchTheHourlyRowsExactly(unittest.TestCase):
    """If the keys disagree by so much as a formatting detail, the
    consumer looks up nothing, falls back to the mean, and the fix is
    silently inert. Worth its own test rather than trusting that two
    copies of the same key construction stay in step."""

    def test_keys_are_identical_for_a_full_day(self):
        hours = _quarter_hour_grid(24)
        soc = np.linspace(20.0, 80.0, len(hours))
        rows = _hourly_means_by_key(
            hours=hours, per_period={"soc_pct": soc}, day_start=DAY
        )
        boundaries = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=20.0, day_start=DAY
        )
        self.assertEqual(list(rows.keys()), list(boundaries.keys()))
        self.assertEqual(len(rows), 24)

    def test_keys_are_identical_for_a_longer_than_one_day_window(self):
        """#356's arbitrary-window case. The two key builders must agree
        there too, not just on the 24-hour path."""
        hours = _quarter_hour_grid(30)
        soc = np.linspace(0.0, 100.0, len(hours))
        rows = _hourly_means_by_key(
            hours=hours, per_period={"soc_pct": soc}, day_start=DAY
        )
        boundaries = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=0.0, day_start=DAY
        )
        self.assertEqual(list(rows.keys()), list(boundaries.keys()))
        self.assertEqual(len(rows), 30)

    def test_keys_are_identical_in_utc_too(self):
        """The key construction steps through UTC for #368's DST reason.
        A UTC day_start is the degenerate case and must still line up."""
        hours = _quarter_hour_grid(4)
        soc = np.linspace(0.0, 10.0, len(hours))
        day_utc = datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC)
        rows = _hourly_means_by_key(
            hours=hours, per_period={"soc_pct": soc}, day_start=day_utc
        )
        boundaries = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=0.0, day_start=day_utc
        )
        self.assertEqual(list(rows.keys()), list(boundaries.keys()))


class TestTheArtifactIsRealAndQuantified(unittest.TestCase):
    """The magnitude claim, measured rather than asserted."""

    def test_on_a_ramp_the_mean_sits_about_half_a_ramp_above_the_boundary(self):
        """A SoC climbing linearly at R points/hour has an hourly mean
        about R/2 above its value at the hour's start. That offset is the
        artifact: it is charged to "sensor disagreement" even when the
        real sensor tracks perfectly."""
        ramp_pct_per_hour = 12.0
        hours = _quarter_hour_grid(6)
        # end-of-period SoC for a constant ramp: period t ends at
        # (t+1) * 0.25 hours elapsed.
        elapsed = (np.arange(len(hours)) + 1) * 0.25
        soc = 10.0 + ramp_pct_per_hour * elapsed

        rows = _hourly_means_by_key(
            hours=hours, per_period={"soc_pct": soc}, day_start=DAY
        )
        boundaries = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=10.0, day_start=DAY
        )
        offsets = [rows[k]["soc_pct"] - boundaries[k] for k in rows if k in boundaries]
        # Mean of the four end-of-period samples in an hour is the
        # boundary plus (0.25+0.5+0.75+1.0)/4 = 0.625 of a ramp.
        expected = ramp_pct_per_hour * 0.625
        for off in offsets:
            self.assertAlmostEqual(off, expected, places=6)
        self.assertGreater(
            expected,
            7.0,
            "a 12 pt/h ramp should inject enough artifact to matter "
            "against an 8.0 mean threshold",
        )


class TestTheDiscrepancyStatUsesTheBoundarySample(unittest.TestCase):
    """End-to-end through `_soc_discrepancy_stats()` itself."""

    @staticmethod
    def _ramp_day(ramp_pct_per_hour: float = 12.0, n_hours: int = 6):
        """A day where the real sensor tracks the achieved trajectory
        PERFECTLY. Any reported discrepancy is therefore pure artifact --
        there is no real disagreement to find."""
        hours = _quarter_hour_grid(n_hours)
        elapsed = (np.arange(len(hours)) + 1) * 0.25
        soc = 10.0 + ramp_pct_per_hour * elapsed
        rows = _hourly_means_by_key(
            hours=hours, per_period={"soc_pct": soc}, day_start=DAY
        )
        boundaries = _soc_pct_at_hour_boundaries(
            hours=hours, soc_pct=soc, initial_soc_pct=10.0, day_start=DAY
        )
        # The real sensor: the same trajectory, sampled densely, as
        # (datetime, pct) pairs — exactly what a recorder read returns.
        hist = [
            (DAY + timedelta(minutes=15 * (i + 1)), float(soc[i]))
            for i in range(len(soc))
        ]
        hist.insert(0, (DAY, 10.0))
        capacity = 100.0
        return rows, boundaries, [(hist, capacity)]

    def test_a_perfectly_tracking_sensor_reports_essentially_no_gap(self):
        rows, boundaries, battery_socs = self._ramp_day()
        out = solver_writer._soc_discrepancy_stats(
            battery_socs, rows, ach_soc_pct_at_hour=boundaries
        )
        self.assertEqual(out["soc_discrepancy_basis"], "hour_boundary")
        self.assertLess(
            float(out["soc_discrepancy_max_pct"]),
            0.5,
            "a real sensor that tracks the achieved trajectory exactly "
            "must not be reported as disagreeing with it",
        )
        self.assertTrue(out["soc_discrepancy_reliable"])

    def test_the_same_day_reported_a_real_gap_before_the_fix(self):
        """The pre-fix path, reached by passing no boundary mapping. This
        is the issue: a perfectly tracking sensor reported as disagreeing,
        purely because a mean was differenced against an instant."""
        rows, _boundaries, battery_socs = self._ramp_day()
        out = solver_writer._soc_discrepancy_stats(
            battery_socs, rows, ach_soc_pct_at_hour=None
        )
        self.assertEqual(out["soc_discrepancy_basis"], "hourly_mean")
        self.assertGreater(
            float(out["soc_discrepancy_max_pct"]),
            5.0,
            "the pre-fix comparison should show the artifact; if it does "
            "not, this scenario no longer demonstrates the issue",
        )

    def test_a_genuine_disagreement_is_still_caught(self):
        """The fix must not blunt the statistic. A real sensor offset by
        20 points still has to trip the 15.0 max threshold.

        Shifted UP, over a 4-hour window, so every value on both sides
        stays inside [0, 100]: an offset that leaves the physical range
        trips the more fundamental `out_of_range` test instead, which
        would make this a test of the wrong branch."""
        rows, boundaries, battery_socs = self._ramp_day(n_hours=4)
        (hist, capacity) = battery_socs[0]
        shifted = [(t, v + 20.0) for t, v in hist]
        out = solver_writer._soc_discrepancy_stats(
            [(shifted, capacity)], rows, ach_soc_pct_at_hour=boundaries
        )
        self.assertGreater(float(out["soc_discrepancy_max_pct"]), 15.0)
        self.assertFalse(out["soc_discrepancy_reliable"])
        self.assertEqual(out["soc_discrepancy_reason"], "disagreement")

    def test_a_partial_mapping_is_reported_as_mixed(self):
        """An hour the boundary mapping does not cover falls back to the
        mean for that hour. The basis must say `mixed` rather than claim
        the whole day was compared like-for-like."""
        rows, boundaries, battery_socs = self._ramp_day()
        partial = dict(list(boundaries.items())[:2])
        out = solver_writer._soc_discrepancy_stats(
            battery_socs, rows, ach_soc_pct_at_hour=partial
        )
        self.assertEqual(out["soc_discrepancy_basis"], "mixed")

    def test_the_honest_absence_path_still_names_the_basis(self):
        """No SoC sensor anywhere: every stat is None, and the basis is
        None too rather than absent -- a consumer reading the key should
        not have to handle it being missing on only some paths."""
        out = solver_writer._soc_discrepancy_stats([], {}, ach_soc_pct_at_hour={})
        self.assertIsNone(out["soc_discrepancy_max_pct"])
        self.assertIn("soc_discrepancy_basis", out)
        self.assertIsNone(out["soc_discrepancy_basis"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
