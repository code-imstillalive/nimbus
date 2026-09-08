"""nimbus issue #479 (sub-issue 3/10 of #476): real tests for
custom_components/nimbus_load/load_run_state.py -- the per-load run-state
store and daily quota carry/rollover math. Pure Python module (no HA
imports at module level), so these tests import it directly, same pattern
as this project's other pure-logic modules (elements.py, network.py).

TestComputeRollover / TestEffectiveTargetAndRemaining cover the pure math
in isolation. TestApplyPowerSample covers the per-tick state transition,
including #479's own acceptance criterion: a synthetic 3-day run where
day 2 under-delivers, day 3's target grows by the shortfall, and day 4
(a day that met quota) reverts back down. TestLoadRunStateStore covers
restart-survival (a fresh Store instance reading back a previous
instance's write) using a real in-memory fake, same reasoning as this
project's own _StubStore in tests/_ha_stubs.py.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load")
)
import load_run_state as lrs

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


class TestComputeRollover(unittest.TestCase):
    def test_a_day_that_fully_met_quota_carries_nothing(self):
        carry = lrs.compute_rollover(
            quota_kwh_per_day=4.0,
            carry_prev_kwh=0.0,
            delivered_yesterday_kwh=4.0,
            carry_cap_kwh=10.0,
        )
        self.assertEqual(carry, 0.0)

    def test_a_day_that_undershot_quota_rolls_the_shortfall_forward(self):
        # #479's own worked example: 4h/day quota, day 2 only delivers 2.5h
        # -- 1.5h of shortfall should roll into day 3's carry.
        carry = lrs.compute_rollover(
            quota_kwh_per_day=4.0,
            carry_prev_kwh=0.0,
            delivered_yesterday_kwh=2.5,
            carry_cap_kwh=10.0,
        )
        self.assertEqual(carry, 1.5)

    def test_a_day_that_overshot_quota_never_goes_negative(self):
        carry = lrs.compute_rollover(
            quota_kwh_per_day=4.0,
            carry_prev_kwh=0.0,
            delivered_yesterday_kwh=6.0,
            carry_cap_kwh=10.0,
        )
        self.assertEqual(carry, 0.0)

    def test_an_overshoot_drains_previously_owed_carry_instead_of_adding_more(self):
        carry = lrs.compute_rollover(
            quota_kwh_per_day=4.0,
            carry_prev_kwh=2.0,
            delivered_yesterday_kwh=5.0,
            carry_cap_kwh=10.0,
        )
        # 4 + 2 - 5 = 1 -- still owed 1h, correctly less than the 2h prior.
        self.assertEqual(carry, 1.0)

    def test_carry_is_clamped_to_the_cap(self):
        carry = lrs.compute_rollover(
            quota_kwh_per_day=4.0,
            carry_prev_kwh=5.0,
            delivered_yesterday_kwh=0.0,
            carry_cap_kwh=6.0,
        )
        # 4 + 5 - 0 = 9, would exceed the 6.0 cap.
        self.assertEqual(carry, 6.0)


class TestEffectiveTargetAndRemaining(unittest.TestCase):
    def test_target_is_quota_plus_carry_with_no_cap(self):
        target = lrs.effective_target_kwh(
            quota_kwh_per_day=4.0, carry_kwh=1.5, max_per_day_kwh=None
        )
        self.assertEqual(target, 5.5)

    def test_target_is_capped_by_max_per_day_but_carry_itself_is_untouched(self):
        # #479's own acceptance criterion: "a day with max_per_day reached
        # carries the remainder again rather than dropping it" -- the cap
        # only limits what the LP is asked to deliver TODAY, not the
        # carry_kwh value itself (which is computed independently by
        # compute_rollover, never derived from effective_target_kwh).
        target = lrs.effective_target_kwh(
            quota_kwh_per_day=4.0, carry_kwh=3.0, max_per_day_kwh=5.0
        )
        self.assertEqual(target, 5.0)

    def test_remaining_is_target_minus_delivered(self):
        remaining = lrs.remaining_kwh(target_kwh=5.5, delivered_today_kwh=2.0)
        self.assertEqual(remaining, 3.5)

    def test_remaining_never_goes_negative(self):
        remaining = lrs.remaining_kwh(target_kwh=4.0, delivered_today_kwh=6.0)
        self.assertEqual(remaining, 0.0)


class TestApplyPowerSample(unittest.TestCase):
    def test_first_ever_sample_turns_the_load_on_with_no_rollover(self):
        state = lrs.LoadRunState()
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        new = lrs.apply_power_sample(state, now=now, day_key="2026-09-07", power_kw=1.5)
        self.assertTrue(new.currently_on)
        self.assertEqual(new.on_since, now.timestamp())
        self.assertIsNone(new.off_since)
        # No prior sample to integrate energy against yet.
        self.assertEqual(new.delivered_today_kwh, 0.0)
        self.assertEqual(new.carry_kwh, 0.0)
        self.assertEqual(new.day_key, "2026-09-07")

    def test_a_second_sample_the_same_day_integrates_energy_over_the_gap(self):
        state = lrs.LoadRunState(
            currently_on=True,
            day_key="2026-09-07",
            last_sample_at=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
        )
        # 30 minutes later, still drawing 1.5kW -- 0.75kWh delivered.
        now = datetime(2026, 9, 7, 8, 30, tzinfo=_TZ)
        new = lrs.apply_power_sample(state, now=now, day_key="2026-09-07", power_kw=1.5)
        self.assertAlmostEqual(new.delivered_today_kwh, 0.75)
        self.assertEqual(new.last_sample_at, now.timestamp())

    def test_a_gap_longer_than_the_cap_is_not_credited_as_delivered_energy(self):
        state = lrs.LoadRunState(
            currently_on=True,
            day_key="2026-09-07",
            last_sample_at=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
        )
        # A 3h gap (a missed cycle, or a restart) -- must NOT credit
        # power_kw * 3h as real delivered energy.
        now = datetime(2026, 9, 7, 11, 0, tzinfo=_TZ)
        new = lrs.apply_power_sample(state, now=now, day_key="2026-09-07", power_kw=1.5)
        self.assertEqual(new.delivered_today_kwh, 0.0)

    def test_power_dropping_to_zero_sets_off_since(self):
        now = datetime(2026, 9, 7, 9, 0, tzinfo=_TZ)
        state = lrs.LoadRunState(
            currently_on=True,
            on_since=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
            day_key="2026-09-07",
            last_sample_at=datetime(2026, 9, 7, 8, 30, tzinfo=_TZ).timestamp(),
        )
        new = lrs.apply_power_sample(state, now=now, day_key="2026-09-07", power_kw=0.0)
        self.assertFalse(new.currently_on)
        self.assertEqual(new.off_since, now.timestamp())
        # on_since is left as the load's own history, not cleared.
        self.assertEqual(new.on_since, state.on_since)

    def test_synthetic_three_day_rollover_matches_479s_own_acceptance_scenario(self):
        # Real solve-tick cadence (~5 min in production) matters here --
        # apply_power_sample()'s own MAX_SAMPLE_GAP_HOURS safety cap
        # (guards against crediting a restart/outage gap as real
        # delivered energy) means a synthetic scenario has to sample at
        # least as often as that cap, not just at each period's start/end.
        quota = 4.0  # kWh/day pool pump target
        cap = 10.0
        step = timedelta(minutes=30)

        def _run_hours(state, start, hours, day_key):
            n_steps = int(hours / 0.5)
            t = start
            for _ in range(n_steps):
                t = t + step
                state = lrs.apply_power_sample(
                    state,
                    now=t,
                    day_key=day_key,
                    power_kw=1.0,
                    quota_kwh_per_day=quota,
                    carry_cap_kwh=cap,
                )
            return state, t

        # Day 1: delivers exactly 4h (quota met) -- first-ever sample, so
        # day_key transitions from "" (no rollover credited).
        state = lrs.LoadRunState()
        day1_start = datetime(2026, 9, 5, 6, 0, tzinfo=_TZ)
        state = lrs.apply_power_sample(
            state, now=day1_start, day_key="2026-09-05", power_kw=1.0
        )
        state, _ = _run_hours(state, day1_start, 4.0, "2026-09-05")
        self.assertAlmostEqual(state.delivered_today_kwh, 4.0)

        # Day 2 begins: rollover fires (day 1 met quota exactly -> carry 0),
        # then only 2.5h gets delivered before the day ends.
        day2_start = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        state = lrs.apply_power_sample(
            state,
            now=day2_start,
            day_key="2026-09-06",
            power_kw=1.0,
            quota_kwh_per_day=quota,
            carry_cap_kwh=cap,
        )
        self.assertEqual(state.carry_kwh, 0.0)
        state, _ = _run_hours(state, day2_start, 2.5, "2026-09-06")
        self.assertAlmostEqual(state.delivered_today_kwh, 2.5)

        # Day 3 begins: rollover from day 2's shortfall (4 - 2.5 = 1.5h
        # carried) -- #479's own worked example. Day 3's own target:
        # quota + carry = 5.5h.
        day3_start = datetime(2026, 9, 7, 6, 0, tzinfo=_TZ)
        state = lrs.apply_power_sample(
            state,
            now=day3_start,
            day_key="2026-09-07",
            power_kw=1.0,
            quota_kwh_per_day=quota,
            carry_cap_kwh=cap,
        )
        self.assertAlmostEqual(state.carry_kwh, 1.5)
        target_day3 = lrs.effective_target_kwh(
            quota_kwh_per_day=quota, carry_kwh=state.carry_kwh, max_per_day_kwh=None
        )
        self.assertAlmostEqual(target_day3, 5.5)
        # Day 3 fully meets its own (grown) target.
        state, _ = _run_hours(state, day3_start, 5.5, "2026-09-07")
        self.assertAlmostEqual(state.delivered_today_kwh, 5.5)

        # Day 4 begins: day 3 met its full (grown) target -> carry reverts
        # to 0, day 4's target reverts back down to the plain 4h quota.
        state = lrs.apply_power_sample(
            state,
            now=datetime(2026, 9, 8, 6, 0, tzinfo=_TZ),
            day_key="2026-09-08",
            power_kw=1.0,
            quota_kwh_per_day=quota,
            carry_cap_kwh=cap,
        )
        self.assertEqual(state.carry_kwh, 0.0)
        target_day4 = lrs.effective_target_kwh(
            quota_kwh_per_day=quota, carry_kwh=state.carry_kwh, max_per_day_kwh=None
        )
        self.assertEqual(target_day4, 4.0)

    def test_non_quota_load_still_resets_delivered_today_daily_with_no_carry(self):
        # A sheddable/deferrable load has no quota_kwh_per_day today (no
        # selectable quota wizard kind yet) -- it still gets a clean
        # per-day delivered_today_kwh reset for #484's future benefit,
        # but never accrues carry_kwh.
        state = lrs.LoadRunState(delivered_today_kwh=3.2, day_key="2026-09-07")
        new = lrs.apply_power_sample(
            state,
            now=datetime(2026, 9, 8, 0, 5, tzinfo=_TZ),
            day_key="2026-09-08",
            power_kw=0.5,
        )
        self.assertEqual(new.carry_kwh, 0.0)
        self.assertEqual(new.day_key, "2026-09-08")
        # Reset happened before this sample's own (tiny, uncredited-
        # first-sample-of-new-day) energy delta.
        self.assertEqual(new.delivered_today_kwh, 0.0)


class TestDecideCommandedState(unittest.TestCase):
    _MIN_HYST = 600.0  # 2 periods @ 5 min, matches #484's own default

    def test_first_ever_decision_adopts_immediately(self):
        state = lrs.LoadRunState()  # commanded_since=None -- never guarded
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        new = lrs.decide_commanded_state(
            state,
            raw_new_state=True,
            now=now,
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertTrue(new.commanded_state)
        self.assertEqual(new.commanded_since, now.timestamp())
        self.assertIsNone(new.pending_state)

    def test_a_raw_value_that_agrees_never_starts_a_challenge(self):
        state = lrs.LoadRunState(
            commanded_state=True,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
        )
        new = lrs.decide_commanded_state(
            state,
            raw_new_state=True,
            now=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ),
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertEqual(new, state)

    def test_a_disagreeing_value_starts_a_challenge_without_flipping_yet(self):
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
        )
        now = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        new = lrs.decide_commanded_state(
            state, raw_new_state=True, now=now, min_hysteresis_seconds=self._MIN_HYST
        )
        self.assertFalse(new.commanded_state)  # not adopted yet
        self.assertTrue(new.pending_state)
        self.assertEqual(new.pending_since, now.timestamp())

    def test_a_challenge_held_short_of_the_threshold_does_not_flip(self):
        challenge_start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
            pending_state=True,
            pending_since=challenge_start.timestamp(),
        )
        # Same challenger, but only 5 minutes in -- short of the 10-minute
        # (2-period) default threshold.
        now = datetime(2026, 9, 7, 8, 5, tzinfo=_TZ)
        new = lrs.decide_commanded_state(
            state, raw_new_state=True, now=now, min_hysteresis_seconds=self._MIN_HYST
        )
        self.assertFalse(new.commanded_state)
        self.assertTrue(new.pending_state)
        self.assertEqual(new.pending_since, challenge_start.timestamp())  # unchanged

    def test_a_challenge_held_past_the_threshold_flips(self):
        challenge_start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
            pending_state=True,
            pending_since=challenge_start.timestamp(),
        )
        now = datetime(2026, 9, 7, 8, 10, tzinfo=_TZ)  # exactly 10 min later
        new = lrs.decide_commanded_state(
            state, raw_new_state=True, now=now, min_hysteresis_seconds=self._MIN_HYST
        )
        self.assertTrue(new.commanded_state)
        self.assertEqual(new.commanded_since, now.timestamp())
        self.assertIsNone(new.pending_state)
        self.assertIsNone(new.pending_since)

    def test_reverting_to_agree_clears_the_challenge_entirely(self):
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
            pending_state=True,
            pending_since=datetime(2026, 9, 7, 8, 5, tzinfo=_TZ).timestamp(),
        )
        # raw_new_state now agrees with commanded_state again -- the
        # in-progress challenge must be dropped, not paused.
        new = lrs.decide_commanded_state(
            state,
            raw_new_state=False,
            now=datetime(2026, 9, 7, 8, 6, tzinfo=_TZ),
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertFalse(new.commanded_state)
        self.assertIsNone(new.pending_state)
        self.assertIsNone(new.pending_since)

    def test_re_challenging_after_reverting_needs_a_full_fresh_window(self):
        # A challenge that almost settled (9 of 10 minutes in), then
        # reverts to agree for one tick, then challenges again -- must
        # need a FULL fresh min_hysteresis_seconds from the restart, not
        # resume from the 9 minutes it had already accumulated. Real
        # scenario this guards against: a load hovering right at its own
        # economic indifference point shouldn't get to "bank" partial
        # progress across separate noise-driven excursions.
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 7, 0, tzinfo=_TZ).timestamp(),
            pending_state=True,
            pending_since=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
        )
        # Reverts to agree at 8:09 (1 minute short of the 10-minute
        # threshold) -- clears the challenge.
        state = lrs.decide_commanded_state(
            state,
            raw_new_state=False,
            now=datetime(2026, 9, 7, 8, 9, tzinfo=_TZ),
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertIsNone(state.pending_state)
        # Challenges again at 8:10 -- a NEW challenge, not a resumption.
        restart = datetime(2026, 9, 7, 8, 10, tzinfo=_TZ)
        state = lrs.decide_commanded_state(
            state,
            raw_new_state=True,
            now=restart,
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertEqual(state.pending_since, restart.timestamp())
        # 9 minutes after the restart (18 min after the ORIGINAL
        # challenge began) -- still short of a fresh 10-minute window,
        # must not have flipped yet.
        new = lrs.decide_commanded_state(
            state,
            raw_new_state=True,
            now=restart + timedelta(minutes=9),
            min_hysteresis_seconds=self._MIN_HYST,
        )
        self.assertFalse(new.commanded_state)

    def test_484s_own_acceptance_scenario_ten_alternating_solves_produce_at_most_one_change(
        self,
    ):
        # #484's own acceptance criterion, verbatim: "Ten consecutive
        # solves that flip an indifferent load's period-0 decision
        # produce <= 1 commanded-state change." Solves 5 minutes apart
        # (a real solve-tick cadence), raw_new_state alternating every
        # single solve -- the load is genuinely indifferent, LP-level
        # numerical noise decides period-0 each time.
        state = lrs.LoadRunState(
            commanded_state=False,
            commanded_since=datetime(2026, 9, 7, 6, 0, tzinfo=_TZ).timestamp(),
        )
        start = datetime(2026, 9, 7, 8, 0, tzinfo=_TZ)
        real_changes = 0
        raw = True
        for i in range(10):
            now = start + timedelta(minutes=5 * i)
            new = lrs.decide_commanded_state(
                state, raw_new_state=raw, now=now, min_hysteresis_seconds=self._MIN_HYST
            )
            if new.commanded_state != state.commanded_state:
                real_changes += 1
            state = new
            raw = not raw
        self.assertLessEqual(real_changes, 1)


class _FakeStore:
    """Real in-memory stand-in for homeassistant.helpers.storage.Store,
    same shape/reasoning as tests/_ha_stubs.py's own _StubStore -- keyed
    by the literal `key` string so two instances built with the same key
    share the same underlying data, matching two real Store objects
    pointing at the same `.storage/<key>` file."""

    _shared_data: ClassVar[dict] = {}

    def __init__(self, key: str) -> None:
        self._key = key
        self._shared_data.setdefault(key, None)

    async def async_load(self):
        return self._shared_data.get(self._key)

    async def async_save(self, data) -> None:
        self._shared_data[self._key] = data


class TestLoadRunStateStore(unittest.TestCase):
    def test_reading_a_never_written_key_returns_a_fresh_state(self):
        store = lrs.LoadRunStateStore(store=_FakeStore("test_never_written"))
        state = asyncio.run(store.async_read("load_1"))
        self.assertEqual(state, lrs.LoadRunState())

    def test_a_write_survives_a_brand_new_store_instance_pointed_at_the_same_key(self):
        # #479's own acceptance criterion: "a restart mid-day does not
        # reset delivered_today (store survives, same guarantee as #342)".
        key = "test_restart_survival"
        store1 = lrs.LoadRunStateStore(store=_FakeStore(key))
        written = lrs.LoadRunState(
            currently_on=True,
            delivered_today_kwh=2.75,
            carry_kwh=1.0,
            day_key="2026-09-07",
        )
        asyncio.run(store1.async_write("load_1", written))

        # A fresh Store + LoadRunStateStore instance, as a real restart
        # would construct -- same key, no shared Python object.
        store2 = lrs.LoadRunStateStore(store=_FakeStore(key))
        restored = asyncio.run(store2.async_read("load_1"))
        self.assertEqual(restored, written)

    def test_two_different_loads_in_the_same_store_do_not_clobber_each_other(self):
        key = "test_multi_load"
        store = lrs.LoadRunStateStore(store=_FakeStore(key))
        asyncio.run(
            store.async_write("load_a", lrs.LoadRunState(delivered_today_kwh=1.0))
        )
        asyncio.run(
            store.async_write("load_b", lrs.LoadRunState(delivered_today_kwh=9.0))
        )
        state_a = asyncio.run(store.async_read("load_a"))
        state_b = asyncio.run(store.async_read("load_b"))
        self.assertEqual(state_a.delivered_today_kwh, 1.0)
        self.assertEqual(state_b.delivered_today_kwh, 9.0)


if __name__ == "__main__":
    unittest.main()
