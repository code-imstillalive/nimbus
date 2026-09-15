"""nimbus issue #875: Nimbus re-sends a command the device is not
following — and a re-send must never count against the activations cap.

Household decision, 2026-09-15, after this issue's own thread established
why the load sat idle: dispatch is edge-triggered, so once
`commanded_state` goes true and stays true, nothing ever repeats the
instruction. A real HWS sat at 5 W standby for 14+ hours while the
dashboard said it was on.

Their decision was two-part, and the second half is the one with teeth:

> yes, re-send, and no, it must not count against the cap

That distinction — **re-affirming a command already given** versus **a
new activation** — did not exist in the code at all, and its absence is
exactly why the only safe behaviour was to never repeat oneself. #534
caps performance activations at a real device-side limit, so a re-send
that consumed one would burn a scarce physical resource on a command
already issued, and the cap would then block the very retry that was
needed.

The headline test here is therefore not about timing at all. It is
`test_a_reaffirm_never_touches_the_activation_count`.

Also covered: the two triggers are deliberately different. A device that
merely diverged gets a patient, spaced re-send — Nimbus does not know
whether repeating itself will help, since something else may be
asserting control. A dispatch that *failed to send* is retried at once,
because Nimbus knows its own command never went out, so there is no
relay to chatter and nothing to be careful about.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import _solver_path  # noqa: F401
from load_run_state import (
    DEFAULT_MAX_REAFFIRMS_PER_DAY,
    DEFAULT_REAFFIRM_AFTER_SECONDS,
    LoadRunState,
    reaffirm_allowed,
    record_reaffirm,
)

_DAY = "2026-09-15"
_NOW = 1_800_000_000.0
_INTERVAL = DEFAULT_REAFFIRM_AFTER_SECONDS  # 15 minutes


def _diverged(seconds_ago: float, **overrides) -> LoadRunState:
    """A load commanded ON whose power sensor says it is off, and has
    been for `seconds_ago`. `command_divergence_seconds()` measures from
    the LATER of the two transitions, so both are set."""
    base = LoadRunState(
        commanded_state=True,
        commanded_since=_NOW - seconds_ago,
        currently_on=False,
        off_since=_NOW - seconds_ago,
        day_key=_DAY,
    )
    return replace(base, **overrides) if overrides else base


def _allowed(state: LoadRunState, **kw) -> bool:
    return reaffirm_allowed(state, now_ts=_NOW, day_key=_DAY, **kw)


class TestTheCapIsNeverConsumedByAReminder(unittest.TestCase):
    """The household's decision, asserted directly. Everything else in
    this file is timing; this is the part that protects real hardware."""

    def test_a_reaffirm_never_touches_the_activation_count(self):
        state = _diverged(
            _INTERVAL * 2, activations_today=3, activations_today_day_key=_DAY
        )
        after = record_reaffirm(state, now_ts=_NOW, day_key=_DAY)

        self.assertEqual(
            after.activations_today,
            3,
            "a re-send consumed one of #534's device-side activations -- a "
            "reminder is not a new activation, and the cap would then block "
            "the retry that was needed",
        )
        self.assertEqual(after.activations_today_day_key, _DAY)

    def test_it_counts_on_its_own_separate_tally(self):
        state = _diverged(_INTERVAL * 2)
        after = record_reaffirm(state, now_ts=_NOW, day_key=_DAY)
        self.assertEqual(after.reaffirms_today, 1)
        self.assertEqual(after.last_reaffirm_at, _NOW)


class TestADivergedDeviceIsRemindedPatiently(unittest.TestCase):
    def test_no_reaffirm_while_the_device_agrees(self):
        agreeing = LoadRunState(
            commanded_state=True,
            commanded_since=_NOW - 9999,
            currently_on=True,
            on_since=_NOW - 9999,
            day_key=_DAY,
        )
        self.assertFalse(_allowed(agreeing))

    def test_no_reaffirm_before_the_interval_has_passed(self):
        """A heat pump takes minutes to show any draw at all. Re-sending
        at the first sign of disagreement would be shouting at a device
        that is simply still waking up."""
        self.assertFalse(_allowed(_diverged(_INTERVAL - 1)))

    def test_reaffirm_once_the_interval_has_passed(self):
        self.assertTrue(_allowed(_diverged(_INTERVAL + 1)))

    def test_reaffirms_are_spaced_not_repeated_every_cycle(self):
        """#484's relay-chatter guard is the entire reason dispatch is
        edge-triggered. Re-affirmation must not reintroduce it."""
        just_sent = _diverged(_INTERVAL * 4, last_reaffirm_at=_NOW - 60)
        self.assertFalse(_allowed(just_sent))

    def test_a_second_reaffirm_is_allowed_after_another_interval(self):
        due_again = _diverged(_INTERVAL * 4, last_reaffirm_at=_NOW - _INTERVAL - 1)
        self.assertTrue(_allowed(due_again))


class TestAFailedSendIsRetriedAtOnce(unittest.TestCase):
    """The separate, sharper case: Nimbus knows its own command never
    went out. Nothing was sent, so there is no relay to chatter."""

    def test_no_waiting_for_the_divergence_threshold(self):
        failed = _diverged(1.0, last_dispatch_failed=True)
        self.assertTrue(
            _allowed(failed),
            "a dispatch that never went out should retry on the next cycle, "
            "not wait out a divergence interval",
        )

    def test_retried_even_before_the_device_could_possibly_have_reacted(self):
        """Divergence may legitimately be None here -- the command failed,
        so the device never had anything to follow."""
        failed = LoadRunState(
            commanded_state=True,
            commanded_since=_NOW,
            currently_on=True,
            on_since=_NOW,
            day_key=_DAY,
            last_dispatch_failed=True,
        )
        self.assertTrue(_allowed(failed))

    def test_but_it_still_respects_the_daily_ceiling(self):
        """Otherwise a permanently-failing device retries forever."""
        exhausted = _diverged(
            1.0,
            last_dispatch_failed=True,
            reaffirms_today=DEFAULT_MAX_REAFFIRMS_PER_DAY,
            reaffirms_today_day_key=_DAY,
        )
        self.assertFalse(_allowed(exhausted))


class TestTheDailyCeiling(unittest.TestCase):
    """A backstop against an endless silent argument with whatever else
    is writing to the device -- the reference household's SG-Ready bridge
    writes to the same water heater."""

    def test_it_stops_once_the_cap_is_reached(self):
        exhausted = _diverged(
            _INTERVAL * 4,
            reaffirms_today=DEFAULT_MAX_REAFFIRMS_PER_DAY,
            reaffirms_today_day_key=_DAY,
        )
        self.assertFalse(_allowed(exhausted))

    def test_yesterdays_count_does_not_carry_into_today(self):
        """The #782 self-healing shape: the count is gated on its OWN
        day-key field, so a state whose general day_key moved without a
        reset still corrects itself on the next cycle."""
        stale = _diverged(
            _INTERVAL * 4,
            reaffirms_today=DEFAULT_MAX_REAFFIRMS_PER_DAY,
            reaffirms_today_day_key="2026-09-14",
        )
        self.assertTrue(_allowed(stale))

    def test_recording_on_a_new_day_rolls_the_count_to_one(self):
        stale = _diverged(
            _INTERVAL * 4, reaffirms_today=17, reaffirms_today_day_key="2026-09-14"
        )
        after = record_reaffirm(stale, now_ts=_NOW, day_key=_DAY)
        self.assertEqual(after.reaffirms_today, 1)
        self.assertEqual(after.reaffirms_today_day_key, _DAY)


class TestZeroDisablesItEntirely(unittest.TestCase):
    """The escape hatch, and a trap worth pinning: `divergence >= 0` is
    always true, so a naive implementation would read 0 as 're-send every
    single cycle' -- the precise opposite of what a household setting 0
    intends."""

    def test_zero_never_reaffirms_however_long_the_divergence(self):
        self.assertFalse(_allowed(_diverged(_INTERVAL * 100), reaffirm_after_seconds=0))

    def test_zero_also_suppresses_the_failed_dispatch_retry(self):
        """'Never re-send this load' has to mean never, including the
        case Nimbus is most confident about."""
        failed = _diverged(1.0, last_dispatch_failed=True)
        self.assertFalse(_allowed(failed, reaffirm_after_seconds=0))


class TestPersistenceRoundTrip(unittest.TestCase):
    def test_the_new_fields_survive_a_store_round_trip(self):
        state = record_reaffirm(
            _diverged(_INTERVAL * 2, last_dispatch_failed=True),
            now_ts=_NOW,
            day_key=_DAY,
        )
        restored = LoadRunState.from_dict(state.to_dict())
        self.assertEqual(restored.reaffirms_today, state.reaffirms_today)
        self.assertEqual(restored.reaffirms_today_day_key, _DAY)
        self.assertEqual(restored.last_reaffirm_at, _NOW)
        self.assertTrue(restored.last_dispatch_failed)

    def test_a_state_persisted_before_these_fields_existed_loads_safely(self):
        """Every existing install upgrades into this with no migration:
        the day-key defaults to "", which can never equal a real day, so
        the first roll is correct."""
        old = LoadRunState(commanded_state=True, day_key=_DAY).to_dict()
        for gone in (
            "reaffirms_today",
            "reaffirms_today_day_key",
            "last_reaffirm_at",
            "last_dispatch_failed",
        ):
            old.pop(gone, None)
        restored = LoadRunState.from_dict(old)
        self.assertEqual(restored.reaffirms_today, 0)
        self.assertEqual(restored.reaffirms_today_day_key, "")
        self.assertIsNone(restored.last_reaffirm_at)
        self.assertFalse(restored.last_dispatch_failed)


if __name__ == "__main__":
    unittest.main()
