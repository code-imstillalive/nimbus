"""Direct test coverage for nimbus issue #565: `number.nimbus_solver_
p2p_block_lead_time_minutes` shifts a P2P fixed-export block's own
effective START minute earlier, so the fixed-rate window can begin a
minute or so before the literal configured hour, absorbing real
dispatch-side lag structurally instead of chasing solve latency.

Real, live household observation this issue is filed from: the periodic
solve is phase-locked ~30s after the real NEM boundary (#244), but a P2P
block's own rate has no live-price dependency at all -- there's no real
reason the block itself needs to wait for the literal hour boundary.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


def _grid_times(start: datetime, hours: float, step_minutes: int = 5) -> list[datetime]:
    n = int(hours * 60 / step_minutes)
    return [start + timedelta(minutes=step_minutes * i) for i in range(n)]


class TestLeadTimeDefaultIsANoOp(unittest.TestCase):
    def test_zero_lead_time_matches_the_pre_565_whole_hour_behaviour(self):
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17,
            "solver_p2p_block_1_end_hour": 20,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 16, 45, tzinfo=BRISBANE), hours=1
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        by_time = dict(zip((gt.isoformat() for gt in grid_times), result, strict=True))
        # Real hour boundary only -- 16:45/16:50/16:55 must NOT match
        # with the field left at its 0 default.
        for minute in (45, 50, 55):
            key = datetime(2026, 9, 10, 16, minute, tzinfo=BRISBANE).isoformat()
            self.assertNotAlmostEqual(by_time[key], 11.5)
        # 17:00 onward (within the block) matches, unchanged from
        # before this feature existed.
        self.assertAlmostEqual(
            by_time[datetime(2026, 9, 10, 17, 0, tzinfo=BRISBANE).isoformat()], 11.5
        )

    def test_field_absent_entirely_is_also_a_no_op(self):
        """A cfg dict that predates this field's own existence (an
        already-configured install that hasn't touched the new number
        entity yet) must behave identically to lead_time=0 explicit."""
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17,
            "solver_p2p_block_1_end_hour": 20,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 16, 55, tzinfo=BRISBANE), hours=0.5
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        self.assertTrue(all(r != 11.5 for r in result[:1]))  # 16:55 unmatched


class TestLeadTimeShiftsTheEffectiveStart(unittest.TestCase):
    def test_ten_minute_lead_time_lets_the_block_begin_early(self):
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17,
            "solver_p2p_block_1_end_hour": 20,
            "solver_p2p_block_lead_time_minutes": 10,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 16, 45, tzinfo=BRISBANE), hours=1
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        by_time = dict(zip((gt.isoformat() for gt in grid_times), result, strict=True))
        # 16:45, 16:50 -- still before the shifted 16:50 effective start
        # (17:00 - 10min): 16:45 must stay unmatched.
        self.assertNotAlmostEqual(
            by_time[datetime(2026, 9, 10, 16, 45, tzinfo=BRISBANE).isoformat()], 11.5
        )
        # 16:50 onward -- the shifted effective start, must now match.
        for minute in (50, 55):
            key = datetime(2026, 9, 10, 16, minute, tzinfo=BRISBANE).isoformat()
            self.assertAlmostEqual(by_time[key], 11.5)
        # The real hour boundary itself still matches too, unaffected.
        self.assertAlmostEqual(
            by_time[datetime(2026, 9, 10, 17, 0, tzinfo=BRISBANE).isoformat()], 11.5
        )

    def test_end_hour_is_completely_unaffected_by_lead_time(self):
        """Issue #565's own explicit scope: only the START shifts."""
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17,
            "solver_p2p_block_1_end_hour": 20,
            "solver_p2p_block_lead_time_minutes": 30,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 19, 45, tzinfo=BRISBANE), hours=0.5
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        by_time = dict(zip((gt.isoformat() for gt in grid_times), result, strict=True))
        self.assertAlmostEqual(
            by_time[datetime(2026, 9, 10, 19, 55, tzinfo=BRISBANE).isoformat()], 11.5
        )
        self.assertNotAlmostEqual(
            by_time[datetime(2026, 9, 10, 20, 0, tzinfo=BRISBANE).isoformat()], 11.5
        )

    def test_lead_time_shared_across_all_three_blocks(self):
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17,
            "solver_p2p_block_1_end_hour": 20,
            "solver_p2p_block_2_rate_kw": 8.0,
            "solver_p2p_block_2_start_hour": 21,
            "solver_p2p_block_2_end_hour": 23,
            "solver_p2p_block_lead_time_minutes": 5,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 20, 50, tzinfo=BRISBANE), hours=0.5
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        by_time = dict(zip((gt.isoformat() for gt in grid_times), result, strict=True))
        self.assertAlmostEqual(
            by_time[datetime(2026, 9, 10, 20, 55, tzinfo=BRISBANE).isoformat()], 8.0
        )
        self.assertNotAlmostEqual(
            by_time[datetime(2026, 9, 10, 20, 50, tzinfo=BRISBANE).isoformat()], 8.0
        )


class TestLeadTimeClampsAtMidnight(unittest.TestCase):
    def test_start_hour_zero_with_lead_time_does_not_reach_into_previous_day(self):
        """Documented, deliberate scope limit -- a block starting at
        hour 0 with a nonzero lead time stays a same-day-only
        comparison rather than reaching back into the previous day's
        own final minutes (a real but rare edge case)."""
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 0,
            "solver_p2p_block_1_end_hour": 3,
            "solver_p2p_block_lead_time_minutes": 30,
        }
        grid_times = _grid_times(
            datetime(2026, 9, 10, 23, 45, tzinfo=BRISBANE), hours=0.5
        )
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, grid_times)
        assert result is not None
        by_time = dict(zip((gt.isoformat() for gt in grid_times), result, strict=True))
        # 23:45/23:50/23:55 (the previous day) must NOT match despite
        # being within 30 minutes of the configured 00:00 start.
        for minute in (45, 50, 55):
            key = datetime(2026, 9, 10, 23, minute, tzinfo=BRISBANE).isoformat()
            self.assertNotAlmostEqual(by_time[key], 11.5)


if __name__ == "__main__":
    unittest.main()
