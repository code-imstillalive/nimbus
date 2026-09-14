"""nimbus issue #875 (Mark Purcell, real household): the HWS heat pump
sat at ~5 W standby for 14+ hours while `commanded_state` stayed `true`
throughout, and nothing noticed.

Traced the dispatch path to confirm why: `apply_commanded_state_guard()`
gates every `dispatch_commanded_state()` call on
`new.commanded_state != prev.commanded_state`, and both call sites sit
inside that `if`. Dispatch is edge-triggered by design (#484's
relay-chatter guard), so a device that diverges *after* the command
lands is never corrected — and was never reported either.

Both halves of the comparison were already persisted (`commanded_state`
from the command, `currently_on` from the real power sensor) and simply
never compared. `command_divergence_seconds()` is a pure function over
that existing state: no new field, no Store schema change, and nothing
on the dispatch path changes.

**Deliberately observational.** A blind periodic re-send is wrong in two
ways — it would collide with the `max_activations_per_day` cap (#534's
real device-side constraint), and re-sending every cycle is precisely
what #484 exists to prevent. Whether re-affirmation would even work is
unknown, since something else appears to write to that device too. This
makes the divergence legible so that decision can be made against data.

The `max()` in the implementation is the subtle part, and the reason
these tests lean on it: the divergence begins at the *later* of the two
transitions. `commanded_since` alone over-reports a device that was
already off before the command arrived; `off_since` alone over-reports
one that has been off for hours while Nimbus also wanted it off.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from load_run_state import LoadRunState, command_divergence_seconds

_HOUR = 3600.0


def _state(**kw) -> LoadRunState:
    base = {
        "currently_on": False,
        "commanded_state": False,
        "commanded_since": None,
        "on_since": None,
        "off_since": None,
    }
    base.update(kw)
    return LoadRunState(**base)


class TestAgreementReturnsNone(unittest.TestCase):
    def test_commanded_off_and_really_off_is_none(self):
        s = _state(
            commanded_state=False,
            currently_on=False,
            commanded_since=100.0,
            off_since=50.0,
        )
        self.assertIsNone(command_divergence_seconds(s, 10_000.0))

    def test_commanded_on_and_really_on_is_none(self):
        s = _state(
            commanded_state=True,
            currently_on=True,
            commanded_since=100.0,
            on_since=120.0,
        )
        self.assertIsNone(command_divergence_seconds(s, 10_000.0))

    def test_none_not_zero_so_the_two_cases_stay_distinguishable(self):
        """A consumer charting this wants a gap when there is no
        divergence, not a zero line that looks like 'just started'."""
        agreeing = _state(
            commanded_state=True,
            currently_on=True,
            commanded_since=100.0,
            on_since=100.0,
        )
        just_started = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=100.0,
            off_since=100.0,
        )
        self.assertIsNone(command_divergence_seconds(agreeing, 100.0))
        self.assertEqual(command_divergence_seconds(just_started, 100.0), 0.0)


class TestTheRealIncident(unittest.TestCase):
    """#875's own reported timeline, as the primary fixture."""

    # 2026-09-14, seconds from midnight for legibility.
    COMMANDED_ON = 7 * _HOUR + 20 * 60  # 07:20 commanded_state -> true
    WENT_IDLE = 1 * _HOUR + 41 * 60  # 01:41 compressor -> 5 W standby
    OBSERVED = 16 * _HOUR + 25 * 60  # 16:25 "right now" in the report

    def test_reproduces_the_reported_nine_hours(self):
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=self.COMMANDED_ON,
            off_since=self.WENT_IDLE,
        )
        hours = command_divergence_seconds(s, self.OBSERVED) / _HOUR
        self.assertAlmostEqual(hours, 9.08, places=1)

    def test_uses_the_later_transition_not_the_earlier(self):
        """The device went idle at 01:41, before the 07:20 command. Had
        this measured from `off_since`, it would report ~14.7 h — the
        device's total idle time — rather than the ~9 h Nimbus has
        actually been wrong for. Both numbers are real; only the second
        answers 'how long has the command been unhonoured'."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=self.COMMANDED_ON,
            off_since=self.WENT_IDLE,
        )
        got = command_divergence_seconds(s, self.OBSERVED) / _HOUR
        self.assertLess(got, 14.0, "measured from off_since, not the later transition")


class TestBothDirections(unittest.TestCase):
    def test_commanded_on_but_device_off(self):
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=1000.0,
            off_since=500.0,
        )
        self.assertEqual(command_divergence_seconds(s, 4600.0), 3600.0)

    def test_commanded_off_but_device_still_running(self):
        """The mirror case — a device ignoring an OFF command matters
        just as much, and costs money rather than comfort."""
        s = _state(
            commanded_state=False,
            currently_on=True,
            commanded_since=1000.0,
            on_since=500.0,
        )
        self.assertEqual(command_divergence_seconds(s, 4600.0), 3600.0)

    def test_a_command_issued_after_the_device_transitioned(self):
        """Device went off at t=500, command to turn ON arrived at
        t=1000 — divergence dates from the command, not the transition."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=1000.0,
            off_since=500.0,
        )
        self.assertEqual(command_divergence_seconds(s, 1000.0), 0.0)

    def test_a_device_that_transitioned_after_the_command(self):
        """Command at t=500 honoured, then the device dropped out at
        t=1000 by itself — divergence dates from the drop-out."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=500.0,
            off_since=1000.0,
        )
        self.assertEqual(command_divergence_seconds(s, 1000.0), 0.0)
        self.assertEqual(command_divergence_seconds(s, 1600.0), 600.0)


class TestUnknownStateIsNotAgreement(unittest.TestCase):
    def test_never_commanded_returns_none(self):
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=None,
            off_since=500.0,
        )
        self.assertIsNone(command_divergence_seconds(s, 10_000.0))

    def test_never_sampled_returns_none(self):
        """A load whose power sensor has never reported has no measured
        transition to compare against. Before v0.94.292 auto-discovered
        the power sensor this was the normal case for the real HWS."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=500.0,
            off_since=None,
        )
        self.assertIsNone(command_divergence_seconds(s, 10_000.0))

    def test_a_clock_that_went_backwards_clamps_to_zero(self):
        """Never report a negative duration -- an HA restart or NTP step
        must not produce a nonsense value on a diagnostic sensor."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=9000.0,
            off_since=500.0,
        )
        self.assertEqual(command_divergence_seconds(s, 1000.0), 0.0)


class TestPurity(unittest.TestCase):
    def test_the_state_is_not_mutated(self):
        """It is published from a read-only sensor property on every
        state read -- it must never write."""
        s = _state(
            commanded_state=True,
            currently_on=False,
            commanded_since=500.0,
            off_since=1000.0,
        )
        before = s.to_dict()
        command_divergence_seconds(s, 5000.0)
        self.assertEqual(s.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
