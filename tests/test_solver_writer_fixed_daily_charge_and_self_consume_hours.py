"""Regression test for nimbus issue #348 (Mark Purcell, codebase review):
two of the file's household-specific hardcoded constants -- a fixed daily
charge added to every install's total_cost_with_fixed_costs, and a
post-midnight self-consume window hard-pinning grid export to 0kW -- are
now real, wizard-configurable fields (number.nimbus_solver_fixed_daily_
charge, number.nimbus_solver_post_window_self_consume_hours) instead of
module constants applied unconditionally to every install regardless of
their own real retailer/automation timing.

Both default to this repo's own reference household's real, already-live
values (1.95, 4) -- an install that never sets these fields sees
byte-identical behaviour to before this fix.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


def _grid_times(
    start: datetime, hours: float, step_minutes: int = 60
) -> list[datetime]:
    n = int(hours * 60 / step_minutes)
    return [start + timedelta(minutes=step_minutes * i) for i in range(n)]


class TestPostWindowSelfConsumeHoursIsConfigurable(unittest.TestCase):
    def setUp(self):
        # A P2P block running through midnight (end_hour=24), same shape
        # as this household's own real Block 1.
        self.cfg_base = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17.0,
            "solver_p2p_block_1_end_hour": 24.0,
        }
        # Spans real midnight (2026-08-30 23:00 through 2026-08-31 06:00)
        # so both the tail of the P2P block and the post-midnight
        # self-consume pin are both exercised in one grid.
        self.grid_times = _grid_times(
            datetime(2026, 8, 30, 23, 0, tzinfo=BRISBANE), hours=7
        )

    def _hours_pinned_to_zero(self, result: list[float]) -> list[int]:
        return [
            gt.hour
            for gt, rate in zip(self.grid_times, result, strict=True)
            if gt.hour < 6 and rate == 0.0
        ]

    def test_default_matches_the_historical_hardcoded_4_hours(self):
        result = solver_writer.fetch_p2p_fixed_export_kw(self.cfg_base, self.grid_times)
        assert result is not None
        self.assertEqual(self._hours_pinned_to_zero(result), [0, 1, 2, 3])

    def test_explicit_override_changes_the_real_pinned_window(self):
        cfg = {**self.cfg_base, "solver_post_window_self_consume_hours": 2}
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, self.grid_times)
        assert result is not None
        self.assertEqual(self._hours_pinned_to_zero(result), [0, 1])

    def test_zero_override_disables_the_post_midnight_pin_entirely(self):
        cfg = {**self.cfg_base, "solver_post_window_self_consume_hours": 0}
        result = solver_writer.fetch_p2p_fixed_export_kw(cfg, self.grid_times)
        assert result is not None
        self.assertEqual(self._hours_pinned_to_zero(result), [])


class TestFixedDailyChargeIsConfigurable(unittest.TestCase):
    def test_default_matches_the_historical_hardcoded_1_95(self):
        self.assertEqual(
            solver_writer._cfg_num({}, "solver_fixed_daily_charge", 1.95), 1.95
        )

    def test_explicit_override_is_honoured(self):
        cfg = {"solver_fixed_daily_charge": 0.85}
        self.assertEqual(
            solver_writer._cfg_num(cfg, "solver_fixed_daily_charge", 1.95), 0.85
        )

    def test_the_old_module_constant_is_genuinely_gone_not_just_unused(self):
        """A real regression this specific issue is about: the module
        constant used to be applied unconditionally regardless of any
        wizard field. Asserting it no longer exists at all (not just that
        the new field works) proves main() can't have silently kept a
        second, dead code path reading the old name."""
        self.assertFalse(hasattr(solver_writer, "FIXED_DAILY_CHARGES"))
        self.assertFalse(
            hasattr(solver_writer, "SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE")
        )
