"""nimbus #1082: a day scored before its settlement landed must not stand.

`compute_daily_quality_report()` scores "yesterday", and the publisher runs
off the solve cycle, so the first attempt lands just after local midnight --
before that day's P2P settlement has populated. `real_p2p_dollars` is then 0,
which additionally makes `real_p2p_volume_kwh > 0.01` false, so the
bonus-priced `grid_oracle` rebuild never runs either: `j_ach`, `j_star` and
`j_ref` all go P2P-blind together and the day is scored as though the
household had no P2P arrangement at all.

The idempotency fast path then re-pushed that score verbatim on every later
cycle, so it stood permanently.

Measured on a real install, 2026-09-17: v0.94.378 was still publishing 16 Sep
at EPR **90.6%** with `real_p2p_dollars: 0` twenty-one hours later, where
scoring the identical day once the settlement had landed returned **95.71%**
with the real $14.5364 applied. The whole 5-point gap is the P2P revenue:

    -6.3493 - 14.5364 = -20.8857   (j_ach, published vs rescored)

This is very likely the mechanism behind a long-standing household report --
"I export, and your system tells me my EPR records zero P2P exports" -- which
had been read as a gating bug more than once. It is a timing bug.
`real_p2p_settlement_status` has reported the truth since #1016; nothing was
hiding it, nothing was reading it.

The two things this has to get right, and which these tests pin:

1. **Only genuinely provisional statuses re-score.** `no_sensor_configured`
   and `window_is_not_one_local_calendar_day` are permanent facts about the
   install or the window, not "not yet" -- re-scoring those would burn an
   oracle MIP every cycle, forever, on every install without a settlement
   sensor. That is strictly worse than the bug being fixed.

2. **It cannot loop.** The publisher runs about once a minute and a rescore
   costs a full oracle MIP -- a straight repeat of #773, where a
   multi-minute solve firing repeatedly starved HA's executor badly enough
   to fail backups. Spacing comes from the published `generated_at`, so it
   needs no new state and survives a restart, and the retry window closes on
   its own when `yesterday_key` rolls over at midnight.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 9, 17, 21, 30, tzinfo=BRISBANE)


def _attrs(status, *, generated_minutes_ago=180, **over):
    a = {
        "latest_date": "2026-09-16",
        "real_p2p_settlement_status": status,
        "generated_at": (NOW - timedelta(minutes=generated_minutes_ago)).isoformat(),
    }
    a.update(over)
    return a


class TestOnlyProvisionalStatusesAreRescored(unittest.TestCase):
    def test_a_settled_day_is_kept(self):
        """`applied` means the real figures are in. Nothing to wait for."""
        self.assertTrue(
            solver_writer._keep_published_quality_score(_attrs("applied"), NOW)
        )

    def test_an_install_with_no_settlement_sensor_is_kept(self):
        """The single most important negative case. `no_sensor_configured`
        is permanent -- treating it as "not yet" would re-score every cycle
        forever on every install that has no settlement source, which is a
        far worse failure than the one being fixed."""
        self.assertTrue(
            solver_writer._keep_published_quality_score(
                _attrs("no_sensor_configured"), NOW
            )
        )

    def test_a_non_calendar_day_window_is_kept(self):
        self.assertTrue(
            solver_writer._keep_published_quality_score(
                _attrs("window_is_not_one_local_calendar_day"), NOW
            )
        )

    def test_an_unsettled_day_is_rescored(self):
        self.assertFalse(
            solver_writer._keep_published_quality_score(
                _attrs("no_settlement_entry_for_this_date"), NOW
            )
        )

    def test_an_unreadable_settlement_sensor_is_rescored(self):
        """Transient for the same reason: the sensor may simply have been
        mid-restart or briefly unavailable."""
        self.assertFalse(
            solver_writer._keep_published_quality_score(
                _attrs("settlement_sensor_unreadable"), NOW
            )
        )


class TestItCannotLoop(unittest.TestCase):
    def test_a_recent_provisional_score_is_left_alone(self):
        """The rescore is spaced, not immediate. Without this the publisher
        would re-run the oracle MIP on every solve cycle -- about once a
        minute -- for the whole of any day whose settlement never arrives."""
        self.assertTrue(
            solver_writer._keep_published_quality_score(
                _attrs("no_settlement_entry_for_this_date", generated_minutes_ago=10),
                NOW,
            )
        )

    def test_the_interval_is_the_boundary(self):
        interval_min = int(
            solver_writer._PROVISIONAL_RESCORE_INTERVAL.total_seconds() // 60
        )
        self.assertTrue(
            solver_writer._keep_published_quality_score(
                _attrs(
                    "no_settlement_entry_for_this_date",
                    generated_minutes_ago=interval_min - 1,
                ),
                NOW,
            )
        )
        self.assertFalse(
            solver_writer._keep_published_quality_score(
                _attrs(
                    "no_settlement_entry_for_this_date",
                    generated_minutes_ago=interval_min + 1,
                ),
                NOW,
            )
        )

    def test_a_missing_timestamp_keeps_the_score(self):
        """No `generated_at` means no way to space the retries, and
        re-scoring forever on a timestamp that cannot be read would be a
        worse failure than the one being fixed."""
        a = _attrs("no_settlement_entry_for_this_date")
        del a["generated_at"]
        self.assertTrue(solver_writer._keep_published_quality_score(a, NOW))

    def test_an_unparseable_timestamp_keeps_the_score(self):
        self.assertTrue(
            solver_writer._keep_published_quality_score(
                _attrs("no_settlement_entry_for_this_date", generated_at="not a date"),
                NOW,
            )
        )

    def test_a_naive_timestamp_is_read_as_utc_and_still_spaced(self):
        """A naive `generated_at` cannot come from this publisher, which
        always writes `now.isoformat()` from an aware value. If one ever
        appeared, `parse_iso()` anchors it to UTC (#363) rather than
        raising, so it participates in the interval normally instead of
        being treated as a special case.

        Pinned because the first version of this test asserted the
        opposite -- that a naive value would be kept -- on the assumption
        that the subtraction would raise. It does not, and asserting a
        guess about a helper's behaviour instead of reading it is how a
        test ends up documenting something that was never true.

        00:00 UTC is 10:00 Brisbane, 11.5 h before NOW, so this is well
        past the interval and re-scores.
        """
        self.assertFalse(
            solver_writer._keep_published_quality_score(
                _attrs(
                    "no_settlement_entry_for_this_date",
                    generated_at="2026-09-17T00:00:00",
                ),
                NOW,
            )
        )


class TestThePublisherActuallyFallsThrough(unittest.TestCase):
    """End to end through `publish_daily_quality_report()`: the unit tests
    above pin the decision, this pins that the decision is wired to the
    behaviour. #1016 is the precedent -- its tests asserted the status
    values, all of which were set correctly, while the thing they gated had
    silently stopped running."""

    def _run(self, status, *, minutes_ago=180):
        state = {
            "state": "90.6",
            "attributes": _attrs(status, generated_minutes_ago=minutes_ago),
        }
        with (
            patch.object(solver_writer, "ha_get", return_value=state),
            patch.object(
                solver_writer, "resolve_real_entity_id", side_effect=lambda e: e
            ),
            patch.object(solver_writer, "ha_post_state") as post,
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=None
            ) as compute,
        ):
            solver_writer.publish_daily_quality_report({}, NOW)
        return compute, post

    def test_a_settled_day_does_not_recompute(self):
        """The fast path must still work. This is the every-cycle case and
        an oracle MIP per minute is what it exists to prevent."""
        compute, post = self._run("applied")
        compute.assert_not_called()
        post.assert_called_once()

    def test_an_unsettled_day_recomputes(self):
        """The actual fix: fall through to `compute_daily_quality_report()`
        rather than re-pushing the P2P-blind score for the rest of the day."""
        compute, _post = self._run("no_settlement_entry_for_this_date")
        compute.assert_called_once()

    def test_a_recently_scored_unsettled_day_does_not_recompute(self):
        compute, post = self._run("no_settlement_entry_for_this_date", minutes_ago=5)
        compute.assert_not_called()
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
