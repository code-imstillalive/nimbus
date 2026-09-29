"""IV&V finding (7ad927b..e497a7c pass, nimbus issues #1450 and #1451): two
real defects in `.claude/skills/nimbus-dispatch-report/scripts/score_day.py`,
found while reviewing the two commits that were supposed to fix related
problems in the same file (`00622dd`/#1423, `a950f81`/#1422).

## Finding 1 (#1450): `soc_boundary_is_instant` could be True while most hours fell back

The day-level flag used to be `any(by_hour_last.get(hh, {}).get(soc) is not
None for hh in by_hour_last)` -- True the moment a SINGLE hour anywhere had a
recorder "last" value, even if every boundary this script actually computed
fell back to the smoothed hourly-mean estimate. A day with one instant sample
and twenty-three fallbacks read as fully "instant": the #1423 NOTE (warning
the reader the figures are smoothed) never fired, and `soc_discrepancy_basis`
published `"instant"` in `yesterday.json` when it should have published
`"hourly_mean_estimate"`.

Fixed by tracking, per hour actually looked up, whether that specific
boundary fell back -- `_real_soc_at_boundary()` now returns `(value,
fell_back)`, and the day-level flag is `not any fell_back`, computed from
what was actually used, not a blanket scan of every hour `by_hour_last`
happens to carry.

## Finding 2 (#1451): the P2P settlement gate withheld a no-P2P install's scorecard forever

`a950f81` (#1422) added a real, needed gate: don't publish a scorecard until
`real_p2p_settlement_status == "applied"`, because a day's own P2P settlement
can still be pending when this report runs, and `j_ach` (and everything
derived from it -- EPR, regret) can move by a lot once it lands.

But `solver_writer.py`'s own `_PROVISIONAL_SETTLEMENT_STATUSES` docstring
distinguishes 5 real status values into 3 classes: `"applied"` (final), two
genuinely time-bound statuses that resolve on their own
(`"no_settlement_entry_for_this_date"`, `"settlement_sensor_unreadable"`),
and two PERMANENT statuses that will never become `"applied"` because there
is no settlement to wait for (`"no_sensor_configured"`,
`"window_is_not_one_local_calendar_day"`). The binary `== "applied"` check
collapsed all four non-"applied" values into the same "withhold pending
settlement" bucket, so an install with no P2P sensor configured at all --
which already publishes final, correctly-zero-credit figures -- would have
had its scorecard withheld every single day, forever.

Fixed with `_settlement_is_final()`, which also accepts the two permanent
statuses.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "nimbus-dispatch-report"
    / "scripts"
    / "score_day.py"
)
_spec = importlib.util.spec_from_file_location("score_day", _SCRIPT_PATH)
score_day = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_day)


class TestSettlementIsFinal(unittest.TestCase):
    def test_applied_is_final(self):
        self.assertTrue(score_day._settlement_is_final("applied"))

    def test_the_two_permanent_statuses_are_final(self):
        for status in (
            "no_sensor_configured",
            "window_is_not_one_local_calendar_day",
        ):
            with self.subTest(status=status):
                self.assertTrue(
                    score_day._settlement_is_final(status),
                    f"{status!r} never becomes 'applied' (no settlement is "
                    "coming), so it must be treated as final -- nimbus #1451",
                )

    def test_the_two_genuinely_provisional_statuses_are_not_final(self):
        for status in (
            "no_settlement_entry_for_this_date",
            "settlement_sensor_unreadable",
            None,
            "",
            "some_future_status_never_seen_before",
        ):
            with self.subTest(status=status):
                self.assertFalse(score_day._settlement_is_final(status))


class TestRealSocAtBoundary(unittest.TestCase):
    def test_uses_the_preceding_hours_last_value_when_present(self):
        by_hour_last = {"09": {"soc": 55.5}}
        by_hour = {"10": {"soc": 40.0}}
        value, fell_back = score_day._real_soc_at_boundary(
            "10", by_hour_last=by_hour_last, by_hour=by_hour, soc_entity="soc"
        )
        self.assertEqual(value, 55.5)
        self.assertFalse(fell_back)

    def test_falls_back_to_the_hourly_mean_on_a_data_gap(self):
        by_hour_last = {}  # no "last" data anywhere -- a real gap
        by_hour = {"10": {"soc": 40.0}}
        value, fell_back = score_day._real_soc_at_boundary(
            "10", by_hour_last=by_hour_last, by_hour=by_hour, soc_entity="soc"
        )
        self.assertEqual(value, 40.0)
        self.assertTrue(fell_back)

    def test_a_partial_gap_is_reported_as_a_fallback_for_that_hour_only(self):
        """The exact shape of nimbus #1450: hour 05's own preceding bucket
        (04) has no "last" reading (a real data gap) while every other
        hour's preceding bucket does. Hour 05 must report fell_back=True
        even though the day overall has plenty of instant data -- which is
        exactly what the old any(...)-over-every-hour flag could not see."""
        # Note: "04" is deliberately absent -- that is the gap.
        by_hour_last = {h: {"soc": 50.0} for h in ("03", "06", "07")}
        by_hour = {"05": {"soc": 33.3}}
        value, fell_back = score_day._real_soc_at_boundary(
            "05", by_hour_last=by_hour_last, by_hour=by_hour, soc_entity="soc"
        )
        self.assertEqual(value, 33.3)
        self.assertTrue(
            fell_back,
            "hour 05's own preceding bucket (04) has no last reading in "
            "this fixture, so this boundary must be reported as a "
            "fallback regardless of every other hour having one",
        )


if __name__ == "__main__":
    unittest.main()
