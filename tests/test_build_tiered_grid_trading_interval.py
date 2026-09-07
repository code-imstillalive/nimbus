"""Real regression tests for nimbus issue #451 (Mark Purcell): tier1's
own end used to be a fixed 24h after tier1_start, regardless of what the
upstream price data could actually support at 5-min resolution. Real
finding: Nimbus does not have a genuine 5-minute-resolution forward
price for 24h -- LocalVolts costsFlexUp/earningsFlexUp is genuine 5-min
only for the CURRENT settlement interval, and AEMO's own longer-horizon
PD7DAY predispatch report is natively 30-min. Every "5-min" period
beyond the current+next real NEM trading interval was really just one
of those 30-min numbers stamped six times with a historical bump on
top -- fake precision, paid for in real solver load.

Tier1 is now boundary-snapped to the real current + next NEM trading
interval (:00/:30) instead of a fixed duration. These tests verify the
exact boundary mechanism against hand-computed expected values (not
copied from the issue's own illustrative prose, which had two small
arithmetic slips in its worked examples -- confirmed by tracing its own
suggested implementation sketch directly), plus the invariants the rest
of the codebase depends on: tier1_end always lands on a clean :00/:30
mark, tier2 starts immediately with zero bridging period, and the whole
grid still totals a genuine 96h horizon from tier1_start.
"""

import unittest
from datetime import datetime, timedelta, timezone

import _solver_path  # noqa: F401
import solver_writer

_TZ = timezone(timedelta(hours=10))


def _grid(now_str):
    now = datetime.fromisoformat(now_str)
    return solver_writer.build_tiered_grid(now)


def _tier1_span(times, hours):
    """Returns (tier1_start, tier1_end, n_tier1_periods) by finding the
    contiguous run of TIER1_PERIOD_HOURS-duration periods."""
    tier1_idxs = [
        i
        for i, h in enumerate(hours)
        if abs(h - solver_writer.TIER1_PERIOD_HOURS) < 1e-9
    ]
    tier1_start = times[tier1_idxs[0]]
    tier1_end = times[tier1_idxs[-1]] + timedelta(
        hours=solver_writer.TIER1_PERIOD_HOURS
    )
    return tier1_start, tier1_end, len(tier1_idxs)


class TestTier1BoundarySnapping(unittest.TestCase):
    """Hand-computed expected values, verified directly against the
    real current+next-trading-interval mechanism -- not copied from
    issue #451's own prose examples (two of which have off-by-one slips
    when traced against its own suggested code)."""

    def test_now_mid_interval_covers_current_plus_next(self):
        # now=14:03 -> tier0 rounds up to 14:05 (next clean 5-min mark).
        # Containing interval [14:00,14:30) ends 14:30; +1 more interval
        # -> 15:00. Span: 14:05 -> 15:00 = 55 min = 11 x 5-min periods.
        times, hours = _grid("2026-09-07T14:03:00+10:00")
        tier1_start, tier1_end, n = _tier1_span(times, hours)
        self.assertEqual(tier1_start.time().isoformat(), "14:05:00")
        self.assertEqual(tier1_end.time().isoformat(), "15:00:00")
        self.assertEqual(n, 11)

    def test_now_exactly_on_a_trading_interval_boundary(self):
        # now=14:00 (already a clean 5-min AND 30-min mark) -> tier1
        # still covers a full current+next: 14:00 -> 15:00 = 60 min.
        times, hours = _grid("2026-09-07T14:00:00+10:00")
        tier1_start, tier1_end, n = _tier1_span(times, hours)
        self.assertEqual(tier1_start.time().isoformat(), "14:00:00")
        self.assertEqual(tier1_end.time().isoformat(), "15:00:00")
        self.assertEqual(n, 12)

    def test_now_just_after_a_trading_interval_boundary(self):
        # now=14:31 -> tier0 rounds up to 14:35. Containing interval
        # [14:30,15:00) ends 15:00; +1 more -> 15:30. Span: 14:35 ->
        # 15:30 = 55 min = 11 periods (this is the case issue #451's
        # own prose said "12 periods" -- traced its own code sketch
        # directly and confirmed 11 is what it actually computes).
        times, hours = _grid("2026-09-07T14:31:00+10:00")
        tier1_start, tier1_end, n = _tier1_span(times, hours)
        self.assertEqual(tier1_start.time().isoformat(), "14:35:00")
        self.assertEqual(tier1_end.time().isoformat(), "15:30:00")
        self.assertEqual(n, 11)

    def test_tier1_end_is_always_a_clean_trading_interval_mark(self):
        # Sweep every real 5-min-aligned starting minute across a full
        # hour -- tier1_end must land on :00 or :30 every single time,
        # never anything else, regardless of where `now` falls.
        for minute in range(0, 60, 5):
            now = datetime(2026, 9, 7, 14, minute, tzinfo=_TZ)
            times, hours = _grid(now.isoformat())
            _, tier1_end, _ = _tier1_span(times, hours)
            self.assertIn(
                tier1_end.minute,
                (0, 30),
                f"now={now.time()} produced tier1_end={tier1_end.time()}, "
                "not on a real :00/:30 trading-interval mark",
            )

    def test_tier1_span_never_exceeds_the_real_max_ceiling(self):
        # "Current + next 30-min trading interval" can never exceed 60
        # real minutes (now landing exactly on a boundary) -- confirms
        # MAX_TIER1_HOURS (used by the report-scoring resolution-
        # matching logic) is a genuine, never-exceeded ceiling, not an
        # arbitrary guess.
        for minute in range(0, 60, 5):
            now = datetime(2026, 9, 7, 14, minute, tzinfo=_TZ)
            times, hours = _grid(now.isoformat())
            tier1_start, tier1_end, _ = _tier1_span(times, hours)
            span_hours = (tier1_end - tier1_start).total_seconds() / 3600.0
            self.assertLessEqual(span_hours, solver_writer.MAX_TIER1_HOURS + 1e-9)


class TestTier2StartsCleanlyWithNoBridge(unittest.TestCase):
    def test_no_bridging_period_between_tier1_and_tier2(self):
        # Old code needed a separate short "bridge" period to reach the
        # next hour mark. tier1_end is now already a clean :00/:30 mark
        # by construction, so tier2 should start immediately at
        # TIER2_PERIOD_HOURS resolution with no odd-duration period in
        # between.
        times, hours = _grid("2026-09-07T14:03:00+10:00")
        _, tier1_end, n_tier1 = _tier1_span(times, hours)
        tier0_count = sum(
            1
            for h in hours
            if abs(h - solver_writer.TIER0_PERIOD_MINUTES / 60.0) < 1e-9
        )
        first_tier2_idx = tier0_count + n_tier1
        self.assertAlmostEqual(
            hours[first_tier2_idx], solver_writer.TIER2_PERIOD_HOURS, places=9
        )
        self.assertEqual(times[first_tier2_idx], tier1_end)

    def test_tier2_resolution_is_30_minutes(self):
        _, hours = _grid("2026-09-07T14:03:00+10:00")
        tier2_hours = [
            h for h in hours if abs(h - solver_writer.TIER2_PERIOD_HOURS) < 1e-9
        ]
        self.assertTrue(tier2_hours)
        for h in tier2_hours:
            self.assertAlmostEqual(h, 0.5, places=9)


class TestTotalHorizonStillNinetySixHours(unittest.TestCase):
    def test_horizon_spans_96_hours_from_tier1_start(self):
        times, hours = _grid("2026-09-07T14:03:00+10:00")
        tier1_start, _, _ = _tier1_span(times, hours)
        last_period_end = times[-1] + timedelta(hours=hours[-1])
        real_span_hours = (last_period_end - tier1_start).total_seconds() / 3600.0
        # The last period may slightly overshoot the nominal 96h mark
        # (same pre-existing "may overshoot by less than one period"
        # property the old tier2 loop already had) -- must not undershoot,
        # and must not overshoot by more than one full tier-2 period.
        self.assertGreaterEqual(real_span_hours, solver_writer.TOTAL_HORIZON_HOURS)
        self.assertLess(
            real_span_hours,
            solver_writer.TOTAL_HORIZON_HOURS + solver_writer.TIER2_PERIOD_HOURS,
        )

    def test_period_count_lands_in_the_expected_reduced_range(self):
        # nimbus issue #451's own estimate: ~365 -> ~200-206 periods.
        # Real, measured range across a full sweep of starting minutes.
        for minute in range(0, 60, 5):
            now = datetime(2026, 9, 7, 14, minute, tzinfo=_TZ)
            times, _ = _grid(now.isoformat())
            self.assertGreater(len(times), 195)
            self.assertLess(len(times), 215)


if __name__ == "__main__":
    unittest.main()
