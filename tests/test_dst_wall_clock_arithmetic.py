"""Regression test for nimbus issue #368 (Mark Purcell, codebase review,
split out of #347): wall-clock `+timedelta` arithmetic on tz-aware LOCAL
datetimes silently breaks across a real DST transition, and Python's own
aware-to-aware comparison is fold-blind whenever both operands share the
identical `tzinfo` object -- the normal case throughout this codebase.

Verified against `Australia/Sydney` for BOTH directions (this project's own
reference household is Brisbane, which never observes DST, so none of this
was ever visible there -- the bug is real for any AEDT/NZ/EU/US install,
which #347 made a supported configuration by making the timezone itself
configurable):

- **Fall-back** (2026-04-05, AEDT->AEST at 03:00 local -> 02:00 local): the
  local wall-clock hour 02:00-03:00 happens TWICE (fold=0 then fold=1, one
  real hour apart). `_build_grid()`/`PeriodGrid.period_starts` used to step
  straight through this in wall-clock space, producing duplicate/
  out-of-order UTC instants; `resample_last_value()`'s bisect used to
  compare the two foldings as EQUAL (same tzinfo object => naive wall-clock
  comparison, fold ignored), silently returning a value from the wrong
  occurrence of the hour.
- **Spring-forward** (2026-10-04, AEST->AEDT at 02:00 local -> 03:00
  local): the local wall-clock hour 02:00-03:00 never happens at all.
  `_build_grid()`/`PeriodGrid.period_starts` used to march straight through
  those nonexistent wall-clock instants, silently fabricating an extra grid
  point (or skipping the true one-hour jump forward), rather than the real,
  correct behaviour of the grid genuinely jumping forward by exactly the
  DST offset.

Fixed by doing every step/accumulation/comparison against the real UTC
instant (`_dst_safe_add()`/`_dst_safe_key()` in ml/model.py, an inline
UTC-round-trip in `PeriodGrid.period_starts` and
`quality_report._hourly_means_by_key()`), converting back to the caller's
own local tzinfo only for the returned values -- every existing caller's
contract (a list of local, ZoneInfo-aware datetimes) is unchanged.

Each test below is written as a genuine mutation check: the assertion
would fail against the pre-fix code (confirmed manually before writing
this file, per this project's own established discipline), not just
"pass trivially either way."
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import _ml_path  # noqa: F401
import _solver_path  # noqa: F401
import numpy as np
from nimbus_load.ml import model as ml_model
from solver.elements import PeriodGrid
from solver.quality_report import _hourly_means_by_key

SYDNEY = ZoneInfo("Australia/Sydney")

# Real AEDT->AEST fall-back: 2026-04-05 03:00 local becomes 02:00 local.
# The wall-clock hour 02:00-03:00 occurs twice.
FALL_BACK_START = datetime(2026, 4, 5, 1, 30, tzinfo=SYDNEY)
FALL_BACK_END = datetime(2026, 4, 5, 4, 0, tzinfo=SYDNEY)

# Real AEST->AEDT spring-forward: 2026-10-04 02:00 local becomes 03:00
# local. The wall-clock hour 02:00-03:00 never happens.
SPRING_FWD_START = datetime(2026, 10, 4, 1, 30, tzinfo=SYDNEY)
SPRING_FWD_END = datetime(2026, 10, 4, 4, 0, tzinfo=SYDNEY)


class TestBuildGridDstTransitions(unittest.TestCase):
    def test_fall_back_grid_covers_every_real_utc_instant_once(self):
        grid = ml_model._build_grid(FALL_BACK_START, FALL_BACK_END, 15)
        utc_instants = [g.astimezone(UTC) for g in grid]
        # Every real UTC instant must be distinct -- the pre-fix wall-clock
        # stepping produced duplicate UTC instants for the repeated hour.
        self.assertEqual(len(utc_instants), len(set(utc_instants)))
        # Strictly increasing in real time, not just non-decreasing.
        for a, b in pairwise(utc_instants):
            self.assertLess(a, b)
        # 01:30 to 04:00 is 2.5 WALL-CLOCK hours, but the repeated
        # 02:00-03:00 hour makes the real ELAPSED span 3.5 hours -- 14
        # real 15-min steps + 1 for the inclusive start = 15 points. The
        # real proof is the distinctness/ordering checks above (a pre-fix
        # wall-clock-stepping version would have produced only 11 points,
        # with 4 of them landing on duplicate UTC instants).
        self.assertEqual(len(grid), 15)

    def test_spring_forward_grid_never_fabricates_the_nonexistent_hour(self):
        grid = ml_model._build_grid(SPRING_FWD_START, SPRING_FWD_END, 15)
        utc_instants = [g.astimezone(UTC) for g in grid]
        self.assertEqual(len(utc_instants), len(set(utc_instants)))
        for a, b in pairwise(utc_instants):
            self.assertLess(a, b)
        # Real elapsed time from 01:30 to 04:00 local is only 1.5 hours
        # (the 02:00-03:00 wall-clock hour never happens) -- 7 points at
        # 15-min steps (6 real 15-min steps + the inclusive start). The
        # pre-fix version fabricated wall-clock points inside the
        # nonexistent hour, producing MORE than 7.
        self.assertEqual(len(grid), 7)


class TestResampleLastValueDstFoldBlindness(unittest.TestCase):
    def test_bisect_does_not_confuse_the_two_real_instants_in_the_repeated_hour(self):
        # Two real, hour-apart events that both carry the wall-clock label
        # "02:30" -- fold=0 (AEDT, the FIRST 02:30) and fold=1 (AEST, the
        # SECOND 02:30, one real hour later). Same-tzinfo comparison
        # ignores fold entirely, so the pre-fix bisect treated these as
        # equal/out-of-order.
        first_0230 = datetime(2026, 4, 5, 2, 30, tzinfo=SYDNEY, fold=0)
        second_0230 = datetime(2026, 4, 5, 2, 30, tzinfo=SYDNEY, fold=1)
        self.assertLess(
            first_0230.astimezone(UTC),
            second_0230.astimezone(UTC),
            "fixture sanity check: fold=1 must be the real, later instant",
        )
        events = [(first_0230, 2.0), (second_0230, 9.0)]
        grid = [first_0230, second_0230]
        out = ml_model.resample_last_value(events, grid)
        # Querying AT the first (fold=0) instant must see only that
        # event's own value, not the later fold=1 one.
        self.assertEqual(out[0], 2.0)
        self.assertEqual(out[1], 9.0)


class TestPeriodGridPeriodStartsDstTransitions(unittest.TestCase):
    def test_fall_back_period_starts_are_distinct_and_increasing(self):
        n = 10
        grid = PeriodGrid(hours=np.full(n, 0.25), start=FALL_BACK_START)
        starts = grid.period_starts
        assert starts is not None
        utc_instants = [s.astimezone(UTC) for s in starts]
        self.assertEqual(len(utc_instants), len(set(utc_instants)))
        for a, b in pairwise(utc_instants):
            self.assertLess(a, b)
        # The real elapsed span of 10 * 15min = 2.5h landing on this exact
        # fall-back transition means the LAST period start is a full real
        # hour later in UTC than 10*15min of pure wall-clock stepping
        # would give -- confirms the accumulation is happening in real
        # elapsed time, not wall-clock.
        expected_last_utc = FALL_BACK_START.astimezone(UTC) + timedelta(
            hours=(n - 1) * 0.25
        )
        self.assertEqual(utc_instants[-1], expected_last_utc)

    def test_spring_forward_period_starts_are_distinct_and_increasing(self):
        n = 10
        grid = PeriodGrid(hours=np.full(n, 0.25), start=SPRING_FWD_START)
        starts = grid.period_starts
        assert starts is not None
        utc_instants = [s.astimezone(UTC) for s in starts]
        self.assertEqual(len(utc_instants), len(set(utc_instants)))
        for a, b in pairwise(utc_instants):
            self.assertLess(a, b)
        expected_last_utc = SPRING_FWD_START.astimezone(UTC) + timedelta(
            hours=(n - 1) * 0.25
        )
        self.assertEqual(utc_instants[-1], expected_last_utc)


class TestQualityReportHourlyMeansDstTransitions(unittest.TestCase):
    def _run(self, day_start: datetime) -> dict[str, dict[str, float]]:
        n = 96  # a real 24h day at 15-min resolution
        hours = np.full(n, 0.25)
        per_period = {"load_kw": np.arange(n, dtype=float)}
        return _hourly_means_by_key(
            hours=hours, per_period=per_period, day_start=day_start
        )

    def test_fall_back_day_produces_24_distinct_real_hourly_keys(self):
        day_start = datetime(2026, 4, 5, 0, 0, tzinfo=SYDNEY)
        out = self._run(day_start)
        self.assertEqual(len(out), 24)
        utc_keys = [datetime.fromisoformat(k).astimezone(UTC) for k in out]
        self.assertEqual(len(utc_keys), len(set(utc_keys)))
        for a, b in pairwise(utc_keys):
            self.assertLess(a, b)
        # Whole-hour-aligned wall-clock stepping from midnight happens to
        # still produce 24 distinct STRING labels across this particular
        # fall-back day (both real occurrences of the repeated hour share
        # the same :00-aligned label), which the checks above alone don't
        # catch -- the real bug is that every label from the transition
        # onward silently points at an instant a full real hour EARLIER
        # than it claims. hour 23's real elapsed time from day_start is
        # exactly 23 real hours regardless of any DST transition inside
        # the day (a plain timedelta in UTC), which a wall-clock-only
        # version gets wrong by exactly the 1h fall-back offset.
        expected_last_utc = day_start.astimezone(UTC) + timedelta(hours=23)
        self.assertEqual(utc_keys[-1], expected_last_utc)

    def test_spring_forward_day_produces_24_distinct_real_hourly_keys(self):
        day_start = datetime(2026, 10, 4, 0, 0, tzinfo=SYDNEY)
        out = self._run(day_start)
        self.assertEqual(len(out), 24)
        utc_keys = [datetime.fromisoformat(k).astimezone(UTC) for k in out]
        self.assertEqual(len(utc_keys), len(set(utc_keys)))
        for a, b in pairwise(utc_keys):
            self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main()
