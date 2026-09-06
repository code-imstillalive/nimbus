"""nimbus issue #416 (Mark Purcell): a "load forecast misconfigured"
persistent notification fired by a genuine startup-timing race (the
forecast coordinator hadn't produced its first fresh refresh yet, so
read_load_forecast_sensor()'s own 90%-zeros circular-reference check
tripped on a still-empty/placeholder forecast) used to sit there
indefinitely once the real forecast populated a few cycles later --
nothing ever cleared it, leaving a stale, wrong, scary notification
behind with no way for the household to know it was already stale.

Two real, separate fixes, both covered here:

1. De-dupe on a STABLE key, not the exact formatted message. The
   90%-zeros message embeds live `{nonzero_points}/{len(load_kw)}`
   counts -- comparing the full string meant the SAME underlying
   condition, seen again with different counts on a later cycle, was
   treated as a "new" error and re-notified.
2. Actively dismiss the notification once a later cycle sees a healthy
   forecast again, instead of leaving it to rot.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


class TestErrorKeyDedup(unittest.TestCase):
    def test_same_condition_different_live_counts_does_not_renotify(self):
        """Two solve cycles hitting the identical 90%-zeros condition,
        with different real point counts (e.g. 9/365 then 11/365, both
        well under the 10% threshold) must be recognized as the SAME
        underlying error, not two different ones."""
        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(solver_writer, "ha_call_service") as mock_call,
            ):
                msg1 = (
                    "sensor.x's forecast has only 9/365 non-trivial "
                    "(>0.01 kW) points -- ..."
                )
                msg2 = (
                    "sensor.x's forecast has only 11/365 non-trivial "
                    "(>0.01 kW) points -- ..."
                )
                import re

                solver_writer._notify_load_forecast_error_once(
                    msg1, error_key=re.sub(r"\d+/\d+", "N/N", msg1)
                )
                solver_writer._notify_load_forecast_error_once(
                    msg2, error_key=re.sub(r"\d+/\d+", "N/N", msg2)
                )
                self.assertEqual(
                    mock_call.call_count,
                    1,
                    "the same underlying condition with different live counts "
                    "must not re-notify",
                )

    def test_error_key_defaults_to_the_full_message_when_omitted(self):
        """Backward compatibility: a caller that doesn't pass error_key
        keeps the original exact-message de-dupe behaviour unchanged."""
        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(solver_writer, "ha_call_service") as mock_call,
            ):
                solver_writer._notify_load_forecast_error_once("error A")
                solver_writer._notify_load_forecast_error_once("error A")
                self.assertEqual(mock_call.call_count, 1)
                solver_writer._notify_load_forecast_error_once("error B")
                self.assertEqual(mock_call.call_count, 2)


class TestClearNotificationOnRecovery(unittest.TestCase):
    def test_clears_and_dismisses_when_a_notification_is_outstanding(self):
        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write("some earlier error")
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(solver_writer, "ha_call_service") as mock_call,
            ):
                solver_writer._clear_load_forecast_error_notification_if_needed()
                mock_call.assert_called_once_with(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": "nimbus_solver_load_forecast_error"},
                )
                self.assertFalse(
                    os.path.exists(sentinel),
                    "the sentinel must be removed so a genuinely NEW future "
                    "error notifies again",
                )

    def test_no_op_when_nothing_is_outstanding(self):
        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")  # never created
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(solver_writer, "ha_call_service") as mock_call,
            ):
                solver_writer._clear_load_forecast_error_notification_if_needed()
                mock_call.assert_not_called()

    def test_a_failed_dismiss_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write("some earlier error")
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(
                    solver_writer,
                    "ha_call_service",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                try:
                    solver_writer._clear_load_forecast_error_notification_if_needed()
                except Exception as e:  # noqa: BLE001 -- reporting a clean self.fail() message for genuinely any exception here, not narrowing to a specific type
                    self.fail(
                        f"_clear_load_forecast_error_notification_if_needed raised: {e}"
                    )


if __name__ == "__main__":
    unittest.main()
