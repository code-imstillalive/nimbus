"""IV&V finding (c881b523..6d3a6d0 pass, 2026-09-21): #1177's own
load-nowcast-skill reason split (nimbus issue #1176) is a binary
decision -- "coverage below threshold" versus "anything else is a
scenario solve failure" -- but `compute_load_nowcast_skill()` has a
THIRD `None`-return cause the call site never considers: the input
arrays disagreeing in length with the period grid
(`solver/nowcast_skill.py`, `if any(len(a) != n for a in arrays):
return None`, checked before the coverage gate and before the solve is
ever attempted).

`_load_nowcast_skill_attributes()` (solver_writer.py) recomputes
coverage independently at the call site and labels the result
"coverage_below_threshold" if that recomputed coverage is low, else
"scenario_solve_failed" -- on the stated reasoning that "coverage below
the bar is decidable here; anything else returning None is the
scenario solve". That reasoning is incomplete: a length mismatch also
returns None, independently of whatever the recomputed coverage says,
and gets folded into "scenario_solve_failed" by the same either/or
logic even though no solve was ever attempted.

Currently dormant, not a live bug: the one real call site
(`_compute_report_for_window()`) constructs `periods`, `grid_times`,
`solar_real_kw`, and `load_real_kw` from the same length, so the
mismatch branch is presently unreachable in production. This test
drives the real `nowcast_skill.compute_load_nowcast_skill()` (not
mocked) with a genuine length mismatch and a high measured-sample
count, so the recomputed coverage is NOT below threshold -- pinning
that the call site currently mislabels this as a solve failure it
never attempted.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 9, 19, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)
GRID = [DAY_START + timedelta(hours=i) for i in range(24)]


class _PeriodsWithHours:
    """A real length-bearing stand-in for PeriodGrid -- only `.hours` is
    read before the length check inside compute_load_nowcast_skill()
    returns, so the fuller object isn't needed to reach that branch."""

    hours = np.arange(24, dtype=np.float64)


def _hist(n):
    return [(DAY_START + timedelta(minutes=10 * i), 1.0) for i in range(n)]


def _call_with_real_length_mismatch():
    """Drive the real attribute builder with a genuine array-length
    mismatch, through the REAL compute_load_nowcast_skill() -- not
    mocked, unlike the sibling #1176 test file, so the actual branch
    that produces the None this test cares about is the one exercised."""

    def _fetch(entity_id, start, end):
        del entity_id, start, end
        return _hist(240)

    with (
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(
            solver_writer,
            "resample_history_mean",
            # 24 samples, matching GRID/period_hours below -- only
            # solar_real_kw/load_real_kw (passed directly, not through
            # this mock) carry the mismatched length.
            side_effect=lambda *a, **k: [1.0] * 24,
        ),
    ):
        return solver_writer._load_nowcast_skill_attributes(
            load_sensor="sensor.load",
            load_scale=1.0,
            grid_times=GRID,
            period_hours=1.0,
            periods=_PeriodsWithHours(),
            grid=object(),
            battery=object(),
            # 20 real samples against a 24-period grid/horizon -- the
            # genuine length mismatch compute_load_nowcast_skill()
            # checks for before it ever attempts a scenario solve.
            solar_real_kw=np.zeros(20),
            load_real_kw=np.ones(20),
            day_start=DAY_START,
            day_end=DAY_END,
        )


class TestLengthMismatchIsNotASolveFailure(unittest.TestCase):
    # Was xfail(strict=True) when Mark filed this. Closed in the same
    # pass: the call site now decides length, then coverage, then the
    # solve -- the function's own order -- and labels a mismatch
    # `input_length_mismatch` instead of blaming a solve never run.
    def test_a_genuine_length_mismatch_is_not_labelled_a_solve_failure(self):
        got = _call_with_real_length_mismatch()
        self.assertNotEqual(
            got["load_nowcast_skill_reason"],
            "scenario_solve_failed",
            "a real array-length mismatch was labelled as a scenario "
            "solve failure -- no solve was ever attempted for this "
            "input, so a household reading this reason is pointed at "
            "a solver problem that doesn't exist",
        )


if __name__ == "__main__":
    unittest.main()
