"""Regression tests for nimbus issue #356 (Mark Purcell, codebase review),
item 4 (second half): `network._align_previous_periods()` used to be a
plain nested loop -- for every new period, rescan `old_starts` from index
0 looking for the first match within `_ALIGNMENT_TOLERANCE`. O(n*m) real
datetime comparisons per solve (~133k at this project's own documented
production scale, a ~365-period 96h tiered grid re-solving against a
same-shaped previous plan every cycle).

Fixed to a two-pointer merge: both `new_starts` and `old_starts` are
guaranteed monotonically increasing (PeriodGrid rejects any non-positive
period duration), so the old index that could match a later new period
can never be earlier than the old index that matched an earlier new
period -- `old_idx` only needs to move forward, never reset. O(n+m).

This is a pure performance refactor -- the fix must produce EXACTLY the
same mapping as the original nested loop for every input, not just "a
plausible one". Verified here against a direct reimplementation of the
original nested-loop algorithm (kept local to this test file, not
imported from network.py, so it stays a genuine independent oracle) across
several real scenarios: exact overlap, a shifted/partial overlap (the
real rolling-resolve case), no overlap at all, and the tolerance boundary
itself.
"""

from __future__ import annotations

import random
import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import PeriodGrid
from solver.network import _ALIGNMENT_TOLERANCE, Plan, _align_previous_periods

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _reference_align(periods: PeriodGrid, previous_plan: Plan | None) -> dict[int, int]:
    """Independent reimplementation of the CURRENT algorithm's own
    selection rule (nimbus issue #635: overlap/containment, not exact-
    start matching -- see _align_previous_periods()'s own docstring for
    the full "why") as a plain O(n*m) restart-from-zero scan, not the
    real two-pointer merge -- the correctness oracle this test compares
    the real, optimized `_align_previous_periods()` against. Deliberately
    duplicated here (not imported) so a future accidental change to BOTH
    the real function and this file in the same way can't silently agree
    with itself. Restarting `old_idx` from 0 for every new period (rather
    than carrying it forward like the real two-pointer does) is what
    keeps this a genuinely independent-of-the-optimization check, while
    still selecting exactly the same old period the real function would.
    """
    if previous_plan is None or not previous_plan.is_optimal:
        return {}
    new_starts = periods.period_starts
    old_starts = previous_plan.periods.period_starts
    if new_starts is None or old_starts is None:
        return {}
    old_hours = previous_plan.periods.hours
    mapping: dict[int, int] = {}
    n_old = len(old_starts)
    for new_idx, new_t in enumerate(new_starts):
        old_idx = 0
        while (
            old_idx < n_old - 1
            and old_starts[old_idx] + timedelta(hours=float(old_hours[old_idx]))
            <= new_t + _ALIGNMENT_TOLERANCE
        ):
            old_idx += 1
        old_end = old_starts[old_idx] + timedelta(hours=float(old_hours[old_idx]))
        if (
            old_starts[old_idx] - _ALIGNMENT_TOLERANCE
            <= new_t
            < old_end + _ALIGNMENT_TOLERANCE
        ):
            mapping[new_idx] = old_idx
    return mapping


def _optimal_plan(periods: PeriodGrid) -> Plan:
    n = periods.n_periods
    z = np.zeros(n)
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=z,
        battery_discharge_kw=z,
        battery_soc_kwh=z,
        grid_import_kw=z,
        grid_export_kw=z,
        export_bonus_kw=z,
        solar_used_kw=z,
        solar_curtailed_kw=z,
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=0.0,
        soc_penalty_cost=0.0,
        iterations=1,
    )


def _grid(n: int, start: datetime, step_minutes: float = 5.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, step_minutes / 60.0), start=start)


class TestMatchesReferenceNestedLoop(unittest.TestCase):
    def test_exact_overlap_every_period_matches_itself(self):
        old = _grid(50, BASE)
        new = _grid(50, BASE)
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 50)
        self.assertEqual(actual, {i: i for i in range(50)})

    def test_shifted_partial_overlap_real_rolling_resolve_shape(self):
        # The real rolling-resolve case: a new solve starts a few periods
        # later than the previous one (time has moved on), so only the
        # tail of the old grid overlaps the head of the new grid.
        old = _grid(50, BASE)
        new = _grid(50, BASE + timedelta(minutes=15))  # 3 periods later
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertTrue(len(actual) > 0)
        self.assertEqual(actual[0], 3)

    def test_no_overlap_at_all_produces_empty_mapping(self):
        old = _grid(50, BASE)
        new = _grid(50, BASE + timedelta(days=10))
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {})

    def test_new_grid_entirely_before_old_grid(self):
        # The reverse direction -- new periods all earlier than every old
        # period. old_idx must never need to move backward for this to
        # correctly report no matches.
        old = _grid(50, BASE + timedelta(days=10))
        new = _grid(50, BASE)
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {})

    def test_variable_width_periods_both_grids(self):
        old_hours = np.array([0.25, 0.25, 1.0, 1.0, 0.25, 0.25])
        new_hours = np.array([0.25, 1.0, 1.0, 0.25, 0.25, 0.25])
        old = PeriodGrid(hours=old_hours, start=BASE)
        new = PeriodGrid(hours=new_hours, start=BASE)
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)

    def test_tolerance_boundary_exactly_at_the_edge(self):
        old = _grid(10, BASE)
        # Shift by exactly _ALIGNMENT_TOLERANCE -- must still match.
        new = _grid(10, BASE + _ALIGNMENT_TOLERANCE)
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {i: i for i in range(10)})

    def test_just_past_start_tolerance_still_matches_via_containment(self):
        # nimbus issue #635: a shift just past _ALIGNMENT_TOLERANCE used
        # to produce NO match at all under the old exact-start-only rule
        # -- under overlap/containment, a ~1-second-off period start is
        # still well within the same 5-minute old period's own interval,
        # so it correctly tethers to it instead of going untethered.
        old = _grid(10, BASE)
        new = _grid(10, BASE + _ALIGNMENT_TOLERANCE + timedelta(microseconds=1))
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {i: i for i in range(10)})

    def test_shift_beyond_a_whole_period_width_produces_no_match(self):
        # A genuinely different real period, well past containment: the
        # boundary this project's own alignment tolerance is actually
        # meant to guard.
        old = _grid(10, BASE, step_minutes=5.0)
        new = _grid(10, BASE + timedelta(minutes=5), step_minutes=5.0)
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        # new[0] aligns to old[1] (exact next-period start), not old[0].
        self.assertEqual(actual[0], 1)

    def test_tier0_mid_slot_periods_tether_to_the_5min_period_they_fall_inside(self):
        """nimbus issue #635 (Mark Purcell): the actual reported bug,
        reconstructed from two real consecutive captured plans. Plan A
        (cron-triggered, boundary-aligned): 5-minute periods starting
        17:25, 17:30, ... Plan B (mid-slot, triggered a minute later by
        a second price source per #633): tier-0 1-minute periods 17:26,
        17:27, 17:28, 17:29, THEN back onto the 5-minute grid at 17:30.

        Before this fix, 17:26-17:29 got no alignment entry at all (no
        old period started at those exact instants) -- proximal
        regularization was silently off for exactly them, and the LP
        parked an arbitrary vertex (the 25 kW power limit) there. After
        the fix, all four now tether to Plan A's own 17:25 period (the
        5-minute window they each fall inside), the same stabilizing
        pressure every boundary-aligned period already gets.
        """
        old = PeriodGrid(
            hours=np.full(4, 5.0 / 60.0), start=datetime(2026, 9, 9, 17, 25, tzinfo=UTC)
        )
        new_hours = np.array(
            [1.0 / 60.0, 1.0 / 60.0, 1.0 / 60.0, 1.0 / 60.0, 5.0 / 60.0]
        )
        new = PeriodGrid(
            hours=new_hours, start=datetime(2026, 9, 9, 17, 26, tzinfo=UTC)
        )
        prev = _optimal_plan(old)
        expected = _reference_align(new, prev)
        actual = _align_previous_periods(new, prev)
        self.assertEqual(actual, expected)
        # 17:26, 17:27, 17:28, 17:29 all fall inside old period 0
        # (17:25-17:30) -- all four tether to it.
        self.assertEqual(actual[0], 0)
        self.assertEqual(actual[1], 0)
        self.assertEqual(actual[2], 0)
        self.assertEqual(actual[3], 0)
        # 17:30 exact-matches old period 1 (17:30-17:35), same as before.
        self.assertEqual(actual[4], 1)

    def test_random_shifts_agree_with_reference_across_many_trials(self):
        rng = random.Random(42)
        for _ in range(30):
            old_n = rng.randint(5, 80)
            new_n = rng.randint(5, 80)
            shift_minutes = rng.randint(-200, 200)
            old = _grid(old_n, BASE)
            new = _grid(new_n, BASE + timedelta(minutes=shift_minutes))
            prev = _optimal_plan(old)
            expected = _reference_align(new, prev)
            actual = _align_previous_periods(new, prev)
            self.assertEqual(actual, expected)

    def test_non_optimal_previous_plan_is_still_ignored(self):
        old = _grid(10, BASE)
        new = _grid(10, BASE)
        prev = _optimal_plan(old)
        infeasible_prev = Plan(
            status="infeasible",
            periods=prev.periods,
            battery_charge_kw=prev.battery_charge_kw,
            battery_discharge_kw=prev.battery_discharge_kw,
            battery_soc_kwh=prev.battery_soc_kwh,
            grid_import_kw=prev.grid_import_kw,
            grid_export_kw=prev.grid_export_kw,
            export_bonus_kw=prev.export_bonus_kw,
            solar_used_kw=prev.solar_used_kw,
            solar_curtailed_kw=prev.solar_curtailed_kw,
            sheddable_loads=[],
            adequacy_loads=[],
            total_cost=None,
            soc_penalty_cost=0.0,
            iterations=0,
        )
        self.assertEqual(_align_previous_periods(new, infeasible_prev), {})

    def test_no_calendar_anchor_on_either_grid_is_still_ignored(self):
        old = PeriodGrid(hours=np.full(10, 0.25), start=None)
        new = PeriodGrid(hours=np.full(10, 0.25), start=None)
        prev = _optimal_plan(old)
        self.assertEqual(_align_previous_periods(new, prev), {})
