"""nimbus issue #1463: the quality scorecard went `unavailable` at local
midnight for 6+ hours on a real install.

Mechanism, from the code: at midnight the "already scored" fast path stops
matching, `compute_daily_quality_report()` returned None for the new day, and
the publisher returned WITHOUT pushing -- so after `_STALE_AFTER_SECONDS`
(5 min) the push sensor went `unavailable`, and an `unavailable` read is what
arms #1248's history-truncation ratchet.

Pinned here: the last good report is re-pushed verbatim (its own
`latest_date` still names its day), the WARNING is once per day, and nothing
is held when there is no genuine prior score to hold.
"""

import sys
import unittest
from datetime import datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

# The module that actually owns the publisher solver_writer calls -- not a
# separately-imported copy, whose warned-set would be a different object.
quality = sys.modules[solver_writer.publish_daily_quality_report.__module__]

BRISBANE = solver_writer.LOCAL_TZ
LOGGER_NAME = solver_writer._LOGGER.name
NOW = datetime(2026, 9, 30, 0, 5, tzinfo=BRISBANE)  # just past midnight


def _existing(state=76.78, latest="2026-09-28"):
    return {
        "state": state,
        "attributes": {"latest_date": latest, "epr": 0.7678, "history": {}},
    }


class TestHoldTheLastGoodScore(unittest.TestCase):
    def setUp(self):
        quality._QUALITY_HOLD_WARNED.clear()

    def tearDown(self):
        quality._QUALITY_HOLD_WARNED.clear()

    def _publish(self, existing):
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=None
            ),
            patch.object(solver_writer, "ha_post_state") as post,
            self.assertLogs(LOGGER_NAME, level="DEBUG") as cm,
        ):
            solver_writer.publish_daily_quality_report({}, NOW)
        return post, [r for r in cm.records if r.levelname == "WARNING"]

    def test_an_unscoreable_new_day_keeps_the_previous_report_alive(self):
        prev = _existing()
        post, warns = self._publish(prev)
        post.assert_called_once()
        entity, state, attrs = post.call_args[0]
        self.assertEqual(entity, solver_writer.QUALITY_ENTITY_ID)
        self.assertEqual(state, 76.78)
        self.assertEqual(
            attrs["latest_date"],
            "2026-09-28",
            "the held report must still name its own day",
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("#1463", warns[0].getMessage())

    def test_the_warning_is_once_per_day_but_the_hold_is_every_cycle(self):
        self._publish(_existing())
        post, warns = self._publish(_existing())
        post.assert_called_once()
        self.assertEqual(warns, [])

    def test_nothing_is_held_when_the_sensor_is_already_unavailable(self):
        """No genuine prior score -- re-pushing `unavailable` would publish
        emptiness as if it were a report."""
        post, _ = self._publish(_existing(state="unavailable"))
        post.assert_not_called()

    def test_nothing_is_held_without_a_dated_report(self):
        post, _ = self._publish({"state": 50.0, "attributes": {}})
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
