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
from datetime import UTC, datetime, timedelta, timezone
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

    def test_import_price_now_accumulates_cost_today_alongside_delivered_energy(self):
        # nimbus issue #591: 0.75 kWh delivered (same shape as the
        # existing 30-min-gap test above) at a real 20c/kWh live price
        # must accumulate exactly 0.15 into cost_today.
        state = lrs.LoadRunState(
            currently_on=True,
            day_key="2026-09-07",
            last_sample_at=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
        )
        now = datetime(2026, 9, 7, 8, 30, tzinfo=_TZ)
        new = lrs.apply_power_sample(
            state,
            now=now,
            day_key="2026-09-07",
            power_kw=1.5,
            import_price_now=0.20,
        )
        self.assertAlmostEqual(new.delivered_today_kwh, 0.75)
        self.assertAlmostEqual(new.cost_today, 0.15)

    def test_import_price_now_omitted_leaves_cost_today_untouched(self):
        # A caller with no live price on hand (or every pre-#591 test/
        # caller) must be a genuine no-op on cost_today, not an error and
        # not a silently-wrong $0.00 claim overwriting a real prior value.
        state = lrs.LoadRunState(
            currently_on=True,
            day_key="2026-09-07",
            cost_today=0.42,
            last_sample_at=datetime(2026, 9, 7, 8, 0, tzinfo=_TZ).timestamp(),
        )
        now = datetime(2026, 9, 7, 8, 30, tzinfo=_TZ)
        new = lrs.apply_power_sample(state, now=now, day_key="2026-09-07", power_kw=1.5)
        self.assertAlmostEqual(new.cost_today, 0.42)

    def test_cost_today_resets_to_zero_on_the_same_rollover_as_delivered_today(self):
        state = lrs.LoadRunState(
            delivered_today_kwh=3.2, cost_today=0.87, day_key="2026-09-07"
        )
        new = lrs.apply_power_sample(
            state,
            now=datetime(2026, 9, 8, 0, 5, tzinfo=_TZ),
            day_key="2026-09-08",
            power_kw=0.5,
            import_price_now=0.20,
        )
        self.assertEqual(new.cost_today, 0.0)
        self.assertEqual(new.day_key, "2026-09-08")


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


class TestActivationAllowedAndRecordActivation(unittest.TestCase):
    """nimbus issue #534 item 3: the daily activation cap -- "a cap of 3
    performance activations per 24h", the real bridge's own device-side
    constraint, made configurable per load rather than hard-coded. See
    solver_writer.py's own apply_commanded_state_guard() for how these two
    functions are wired into the real dispatch decision."""

    def test_no_cap_configured_always_allows(self):
        state = lrs.LoadRunState(activations_today=999, day_key="2026-09-08")
        self.assertTrue(
            lrs.activation_allowed(
                state, max_activations_per_day=None, day_key="2026-09-08"
            )
        )

    def test_under_the_cap_is_allowed(self):
        state = lrs.LoadRunState(activations_today=2, day_key="2026-09-08")
        self.assertTrue(
            lrs.activation_allowed(
                state, max_activations_per_day=3, day_key="2026-09-08"
            )
        )

    def test_at_the_cap_is_blocked(self):
        state = lrs.LoadRunState(activations_today=3, day_key="2026-09-08")
        self.assertFalse(
            lrs.activation_allowed(
                state, max_activations_per_day=3, day_key="2026-09-08"
            )
        )

    def test_a_stale_days_count_does_not_count_against_a_new_day(self):
        # activations_today=5 was YESTERDAY's count -- today hasn't rolled
        # over yet (record_activation() is what performs that roll), so
        # activation_allowed() must read today's real count as 0, not 5.
        state = lrs.LoadRunState(activations_today=5, day_key="2026-09-07")
        self.assertTrue(
            lrs.activation_allowed(
                state, max_activations_per_day=3, day_key="2026-09-08"
            )
        )

    def test_record_activation_increments_within_the_same_day(self):
        state = lrs.LoadRunState(activations_today=1, day_key="2026-09-08")
        result = lrs.record_activation(state, day_key="2026-09-08")
        self.assertEqual(result.activations_today, 2)
        self.assertEqual(result.day_key, "2026-09-08")

    def test_record_activation_rolls_over_on_a_new_day(self):
        state = lrs.LoadRunState(activations_today=5, day_key="2026-09-07")
        result = lrs.record_activation(state, day_key="2026-09-08")
        self.assertEqual(result.activations_today, 1)
        self.assertEqual(result.day_key, "2026-09-08")

    def test_record_activation_from_a_never_sampled_state_starts_at_one(self):
        state = lrs.LoadRunState()  # day_key="" -- never sampled
        result = lrs.record_activation(state, day_key="2026-09-08")
        self.assertEqual(result.activations_today, 1)


class TestLoadRunStateActivationsTodayRoundTrip(unittest.TestCase):
    def test_to_dict_and_from_dict_round_trip_activations_today(self):
        state = lrs.LoadRunState(activations_today=2, day_key="2026-09-08")
        restored = lrs.LoadRunState.from_dict(state.to_dict())
        self.assertEqual(restored.activations_today, 2)

    def test_from_dict_defaults_activations_today_to_zero_for_old_data(self):
        # Real backward-compat case: a LoadRunState written before #534
        # shipped has no "activations_today" key in its persisted dict at
        # all -- must not raise, must default to 0.
        old_data = {"currently_on": True, "delivered_today_kwh": 1.5}
        restored = lrs.LoadRunState.from_dict(old_data)
        self.assertEqual(restored.activations_today, 0)


class TestBuildTimeValueSeries(unittest.TestCase):
    """nimbus issue #581: build_time_value_series() -- the shared
    {"time", "value"} shape every Nimbus forecast sensor already uses."""

    def test_pairs_each_grid_time_with_its_own_value_rounded(self):
        times = [
            datetime(2026, 9, 9, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 9, 0, 30, tzinfo=UTC),
        ]
        result = lrs.build_time_value_series(times, [1.23456, 2.0])
        self.assertEqual(
            result,
            [
                {"time": times[0].isoformat(), "value": 1.235},
                {"time": times[1].isoformat(), "value": 2.0},
            ],
        )

    def test_works_with_a_numpy_array_not_just_a_plain_list(self):
        import numpy as np

        times = [datetime(2026, 9, 9, 0, 0, tzinfo=UTC)]
        result = lrs.build_time_value_series(times, np.array([3.5]))
        self.assertEqual(result, [{"time": times[0].isoformat(), "value": 3.5}])

    def test_empty_inputs_produce_an_empty_series(self):
        self.assertEqual(lrs.build_time_value_series([], []), [])


class TestLoadRunStatePlanForecastRoundTrip(unittest.TestCase):
    """nimbus issue #581: the seven new plan_* fields round-trip through
    to_dict()/from_dict() the same way activations_today already does
    above, including the same old-data backward-compat guarantee."""

    def test_to_dict_and_from_dict_round_trip_every_new_field(self):
        state = lrs.LoadRunState(
            plan_forecast=[{"time": "t0", "value": 1.0}],
            plan_delivered_kwh_forecast=[{"time": "t0", "value": 0.5}],
            plan_target_kwh=5.0,
            plan_shortfall_kwh=0.2,
            plan_earliest_period=2,
            plan_deadline_period=12,
            plan_nominal_kw=1.5,
        )
        restored = lrs.LoadRunState.from_dict(state.to_dict())
        self.assertEqual(restored.plan_forecast, state.plan_forecast)
        self.assertEqual(
            restored.plan_delivered_kwh_forecast, state.plan_delivered_kwh_forecast
        )
        self.assertEqual(restored.plan_target_kwh, 5.0)
        self.assertEqual(restored.plan_shortfall_kwh, 0.2)
        self.assertEqual(restored.plan_earliest_period, 2)
        self.assertEqual(restored.plan_deadline_period, 12)
        self.assertEqual(restored.plan_nominal_kw, 1.5)

    def test_from_dict_defaults_every_new_field_to_none_for_old_data(self):
        # Real backward-compat case: a LoadRunState written before #581
        # shipped has none of these seven keys at all -- must not raise,
        # must default to None (not 0.0/[] -- "never computed" is a
        # genuinely different state from "computed as zero/empty").
        old_data = {"currently_on": True, "delivered_today_kwh": 1.5}
        restored = lrs.LoadRunState.from_dict(old_data)
        self.assertIsNone(restored.plan_forecast)
        self.assertIsNone(restored.plan_delivered_kwh_forecast)
        self.assertIsNone(restored.plan_target_kwh)
        self.assertIsNone(restored.plan_shortfall_kwh)
        self.assertIsNone(restored.plan_earliest_period)
        self.assertIsNone(restored.plan_deadline_period)
        self.assertIsNone(restored.plan_nominal_kw)


class TestLoadRunStateThermalAnchorRoundTrip(unittest.TestCase):
    """nimbus issues #609/#610: last_idle_temperature (the settled-idle
    anchor solver_writer.py's own wiring falls back to while a heating
    run is active or still unsettled) and thermal_rates_source ("learned"
    vs "fallback", #610's own transparency ask) round-trip through
    to_dict()/from_dict() the same way #581's plan_* fields already do
    above, including the same old-data backward-compat guarantee."""

    def test_to_dict_and_from_dict_round_trip_both_fields(self):
        state = lrs.LoadRunState(
            last_idle_temperature=53.8,
            thermal_rates_source="learned",
        )
        restored = lrs.LoadRunState.from_dict(state.to_dict())
        self.assertEqual(restored.last_idle_temperature, 53.8)
        self.assertEqual(restored.thermal_rates_source, "learned")

    def test_from_dict_defaults_for_old_data_predating_these_fields(self):
        old_data = {"currently_on": True, "delivered_today_kwh": 1.5}
        restored = lrs.LoadRunState.from_dict(old_data)
        self.assertIsNone(restored.last_idle_temperature)
        self.assertEqual(restored.thermal_rates_source, "")


class TestLoadRunStateShadowPriceForecastRoundTrip(unittest.TestCase):
    """nimbus issue #613 (Mark Purcell, item 1 of 3 -- exposure only):
    plan_shadow_price_forecast round-trips through to_dict()/from_dict()
    the same way #591's own plan_cost_forecast already does."""

    def test_to_dict_and_from_dict_round_trip(self):
        series = [{"time": "t0", "value": 0.0013}, {"time": "t1", "value": 0.0006}]
        state = lrs.LoadRunState(plan_shadow_price_forecast=series)
        restored = lrs.LoadRunState.from_dict(state.to_dict())
        self.assertEqual(restored.plan_shadow_price_forecast, series)

    def test_from_dict_defaults_to_none_for_old_data(self):
        old_data = {"currently_on": True}
        restored = lrs.LoadRunState.from_dict(old_data)
        self.assertIsNone(restored.plan_shadow_price_forecast)


def _iso_grid(start: datetime, n: int, minutes: int = 30) -> list[datetime]:
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


def _series(times: list[datetime], values: list[float]) -> list[dict]:
    return lrs.build_time_value_series(times, values)


class TestFindCurrentOrNextRun(unittest.TestCase):
    """nimbus issue #590: _find_current_or_next_run() is the private
    scan derive_schedule_view() itself relies on to locate next_start/
    next_end -- tested directly, same "test the private helper directly"
    convention this project already uses for _resolve_hour_to_period_
    index() in test_solver_writer_controllable_loads.py."""

    def setUp(self):
        self.times = _iso_grid(datetime(2026, 9, 9, 6, 0, tzinfo=_TZ), 6)

    def test_no_period_ever_exceeds_threshold_returns_none(self):
        forecast = _series(self.times, [0.0] * 6)
        self.assertIsNone(
            lrs._find_current_or_next_run(
                forecast, now=self.times[0], on_threshold_kw=0.05
            )
        )

    def test_empty_forecast_returns_none(self):
        self.assertIsNone(
            lrs._find_current_or_next_run([], now=self.times[0], on_threshold_kw=0.05)
        )

    def test_upcoming_run_found_after_now(self):
        forecast = _series(self.times, [0.0, 0.65, 0.65, 0.0, 0.0, 0.0])
        run = lrs._find_current_or_next_run(
            forecast, now=self.times[0], on_threshold_kw=0.05
        )
        self.assertEqual(run, (1, 2))

    def test_run_already_in_progress_is_found_by_its_own_start(self):
        # now falls INSIDE period 1's own span (06:30 <= now < 07:00) --
        # the run is already running, not merely upcoming, but its own
        # start_idx is still 1 (the period that's actually active).
        forecast = _series(self.times, [0.0, 0.65, 0.65, 0.0, 0.0, 0.0])
        now = self.times[1] + timedelta(minutes=10)
        run = lrs._find_current_or_next_run(forecast, now=now, on_threshold_kw=0.05)
        self.assertEqual(run, (1, 2))

    def test_run_extending_to_the_end_of_the_published_horizon(self):
        forecast = _series(self.times, [0.0, 0.0, 0.0, 0.0, 0.65, 0.65])
        run = lrs._find_current_or_next_run(
            forecast, now=self.times[0], on_threshold_kw=0.05
        )
        self.assertEqual(run, (4, 5))


class TestDeriveScheduleView(unittest.TestCase):
    """nimbus issue #590 (Mark Purcell, real ask on the #534 heat pump's
    own device page: "I don't know if it is scheduled, what time and for
    how long. how much will it cost, what are the forecasts..."): the
    seven-entity view, derived entirely from what #479/#484/#581 already
    persist."""

    def setUp(self):
        self.times = _iso_grid(datetime(2026, 9, 9, 6, 0, tzinfo=_TZ), 6)
        # Periods 1-2 (06:30, 07:00) are the load's own scheduled run --
        # 1.0h at 0.65 kW, 0.65 kWh, matching the delivered-kwh cumulative
        # series below exactly (0.325 kWh credited per half-hour period).
        self.forecast = _series(self.times, [0.0, 0.65, 0.65, 0.0, 0.0, 0.0])
        self.delivered_forecast = _series(
            self.times, [0.0, 0.325, 0.65, 0.65, 0.65, 0.65]
        )

    def test_scheduled_deferrable_run_reports_window_duration_and_energy(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            plan_shortfall_kwh=0.0,
            commanded_state=False,
            delivered_today_kwh=0.0,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.next_start, self.times[1])
        self.assertEqual(view.next_end, self.times[3])
        self.assertAlmostEqual(view.planned_duration_h, 1.0)
        self.assertAlmostEqual(view.planned_energy_kwh, 0.65)
        self.assertEqual(view.target_today_kwh, 0.65)
        self.assertEqual(view.status, "scheduled 06:30–07:30")

    def test_planned_cost_sums_the_cost_forecast_over_the_run_window(self):
        # nimbus issue #591: same run window as the test above (periods
        # 1-2, 0.65 kW for 0.5h each) at a flat 0.12 $/kWh -- 0.325 kWh *
        # $0.12 = $0.039 per period, summed to $0.078. (Chosen to land
        # cleanly on build_time_value_series()'s own 3dp rounding -- a
        # per-period cost of exactly $0.0325 rounds UP to $0.033 there,
        # which would make this test's own expected sum wrong for a
        # reason that has nothing to do with derive_schedule_view()
        # itself; found live when this test first ran on CI.)
        cost_forecast = _series(self.times, [0.0, 0.039, 0.039, 0.0, 0.0, 0.0])
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_cost_forecast=cost_forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertAlmostEqual(view.planned_cost, 0.078)

    def test_planned_cost_is_none_when_the_cost_forecast_was_not_published(self):
        # An unpriced run must read as an honest "unknown," never a
        # fabricated $0.00.
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertIsNone(view.planned_cost)

    def test_planned_today_sums_every_remaining_period_today_not_just_the_next_run(
        self,
    ):
        # nimbus issue #615: TWO separate scheduled blocks today -- the
        # next-run-only planned_energy_kwh must see only the first
        # (period 1, 0.325 kWh), while planned_today_kwh sums both.
        forecast = _series(self.times, [0.0, 0.65, 0.0, 0.65, 0.0, 0.0])
        state = lrs.LoadRunState(
            plan_forecast=forecast,
            plan_target_kwh=1.0,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertAlmostEqual(view.planned_energy_kwh, 0.325)  # next block only
        self.assertAlmostEqual(view.planned_today_kwh, 0.65)  # both blocks

    def test_planned_today_excludes_periods_already_in_the_past(self):
        # "now" sits after period 1's own scheduled run -- only period 3
        # (still ahead) counts toward planned_today.
        forecast = _series(self.times, [0.0, 0.65, 0.0, 0.65, 0.0, 0.0])
        state = lrs.LoadRunState(
            plan_forecast=forecast,
            plan_target_kwh=1.0,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[3]
        )
        self.assertAlmostEqual(view.planned_today_kwh, 0.325)

    def test_planned_today_excludes_periods_past_midnight(self):
        # A period genuinely on TOMORROW's calendar date, still inside
        # the published forecast horizon, must not count toward "today."
        tomorrow_times = _iso_grid(self.times[0] + timedelta(days=1), 2)
        forecast = _series(
            self.times + tomorrow_times, [0.0, 0.65, 0.0, 0.0, 0.0, 0.0, 0.65, 0.0]
        )
        state = lrs.LoadRunState(
            plan_forecast=forecast,
            plan_target_kwh=1.0,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertAlmostEqual(view.planned_today_kwh, 0.325)  # only today's period

    def test_planned_today_is_none_with_no_plan_forecast_at_all(self):
        state = lrs.LoadRunState(commanded_state=False, day_key="2026-09-09")
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertIsNone(view.planned_today_kwh)

    def test_cost_today_passes_through_the_states_own_live_accumulator(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            commanded_state=False,
            delivered_today_kwh=0.325,
            cost_today=0.046,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertAlmostEqual(view.cost_today, 0.046)

    def test_running_takes_priority_over_every_other_status(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            plan_shortfall_kwh=5.0,  # would otherwise say "will miss target"
            commanded_state=True,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "running")

    def test_shortfall_reported_when_deferrable_load_will_miss_target(self):
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=0.5,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "will miss target by 0.50 kWh")

    def test_capped_status_takes_priority_over_a_real_scheduled_run(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            plan_shortfall_kwh=0.0,
            commanded_state=False,
            activations_today=3,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state,
            load_kind="deferrable",
            now=self.times[0],
            max_activations_per_day=3,
        )
        self.assertEqual(view.status, "capped (3/3)")

    def test_activations_from_a_prior_day_do_not_count_toward_todays_cap(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_delivered_kwh_forecast=self.delivered_forecast,
            plan_target_kwh=0.65,
            plan_shortfall_kwh=0.0,
            commanded_state=False,
            activations_today=5,
            day_key="2026-09-08",  # yesterday, relative to `now` below
        )
        view = lrs.derive_schedule_view(
            state,
            load_kind="deferrable",
            now=self.times[0],
            max_activations_per_day=3,
        )
        self.assertNotEqual(view.status, "capped (3/3)")
        self.assertEqual(view.status, "scheduled 06:30–07:30")

    def test_done_status_without_a_tank_reading(self):
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=None,
            commanded_state=False,
            delivered_today_kwh=2.0,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "done")

    def test_done_status_with_a_tank_reading_says_done_when_the_tank_really_is(self):
        # nimbus issue #639: "done (tank X °C)" is only honest when the
        # tank's own done_when condition genuinely fired -- pass
        # done_condition_met=True for that case.
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=None,
            commanded_state=False,
            delivered_today_kwh=2.0,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state,
            load_kind="deferrable",
            now=self.times[0],
            tank_current_temperature=60.3,
            done_condition_met=True,
        )
        self.assertEqual(view.status, "done (tank 60 °C)")

    def test_target_met_but_tank_not_done_says_target_met_not_done(self):
        # nimbus issue #639 (Mark Purcell, live verification): the real
        # bug -- a load released on its kWh target showed "done (tank
        # 52 °C)" right beside a device-page "done at 60 °C" line. The
        # tank (52) was never actually done; only the 2.0 kWh target
        # was. done_condition_met defaults to None (not yet known /
        # genuinely false), which must NOT produce "done" wording.
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=None,
            commanded_state=False,
            delivered_today_kwh=3.54,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state,
            load_kind="deferrable",
            now=self.times[0],
            tank_current_temperature=52.0,
        )
        self.assertEqual(view.status, "target met (3.5 of 2.0 kWh, tank 52 °C)")

    def test_no_tank_reading_at_all_keeps_the_plain_done_wording(self):
        # Scoped fix: #639's ambiguity only exists when there's a tank
        # reading to contradict. A load with no done_entity (or a
        # non-water_heater/climate one) has nothing to contradict, so
        # "done" is still the honest word here -- unchanged from before
        # #639.
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=None,
            commanded_state=False,
            delivered_today_kwh=2.0,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "done")

    def test_done_condition_met_false_also_says_target_met_not_done(self):
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=2.0,
            plan_shortfall_kwh=None,
            commanded_state=False,
            delivered_today_kwh=2.0,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state,
            load_kind="deferrable",
            now=self.times[0],
            tank_current_temperature=52.0,
            done_condition_met=False,
        )
        self.assertEqual(view.status, "target met (2.0 of 2.0 kWh, tank 52 °C)")

    def test_sheddable_load_has_no_target_today(self):
        state = lrs.LoadRunState(
            plan_forecast=self.forecast,
            plan_target_kwh=99.0,  # never populated for real sheddable loads
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(state, load_kind="sheddable", now=self.times[0])
        self.assertIsNone(view.target_today_kwh)

    def test_sheddable_load_reports_shed_energy_from_past_periods(self):
        # Four half-hour periods, all already in the past relative to
        # `now`, each served at 0.6 kW against a 1.0 kW nominal rate --
        # 0.2 kW short x 0.5h x 4 periods = 0.8 kWh shed today.
        past_times = _iso_grid(datetime(2026, 9, 9, 6, 0, tzinfo=_TZ), 4)
        forecast = _series(past_times, [0.6, 0.6, 0.6, 0.6])
        state = lrs.LoadRunState(
            plan_forecast=forecast,
            plan_nominal_kw=1.0,
            commanded_state=False,
            day_key="2026-09-09",
        )
        now = past_times[-1] + timedelta(hours=1)
        view = lrs.derive_schedule_view(state, load_kind="sheddable", now=now)
        self.assertEqual(view.status, "shed 0.80 kWh today")

    def test_outside_window_is_the_honest_fallback(self):
        state = lrs.LoadRunState(
            plan_forecast=_series(self.times, [0.0] * 6),
            plan_target_kwh=None,
            plan_shortfall_kwh=None,
            commanded_state=False,
            day_key="2026-09-09",
        )
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "outside window")
        self.assertIsNone(view.next_start)
        self.assertIsNone(view.next_end)
        self.assertIsNone(view.planned_duration_h)
        self.assertIsNone(view.planned_energy_kwh)

    def test_no_plan_forecast_at_all_is_a_safe_empty_view(self):
        # A load never yet solved this cycle (fresh subentry, or the
        # store simply has nothing for it) -- LoadRunState()'s own
        # all-None defaults must never raise.
        state = lrs.LoadRunState()
        view = lrs.derive_schedule_view(
            state, load_kind="deferrable", now=self.times[0]
        )
        self.assertEqual(view.status, "outside window")
        self.assertEqual(view.delivered_today_kwh, 0.0)


if __name__ == "__main__":
    unittest.main()
