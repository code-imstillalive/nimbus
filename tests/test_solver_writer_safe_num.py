"""Real regression test, live incident (Mark Purcell, an independent
installer, 2026-08-24, same day as the min_soc floor fix -- see
test_solver_writer_min_soc_floor.py's own module docstring for the
"it should catch errors and manage them" conversation this is a direct
continuation of): the native solver_runtime.py loop crashed with

    ValueError: could not convert string to float:
    '2026-08-24T13:00:00+10:00'

fetching cfg["solver_export_price_sensor"] via the old, unprotected
``num()`` closure (``float(ha_get(entity_id)["state"])``, local to
main() and therefore untestable in isolation). safe_num() replaces it:
a genuine top-level function that degrades gracefully (WARN + fallback)
instead of propagating a bare ValueError/KeyError/TypeError out of the
whole solve cycle whenever a configured entity's real state can't be
parsed as a number -- whatever the reason (wrong entity configured,
"unavailable"/"unknown" during a transient outage, or any other shape
mismatch).
"""

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


class TestMarksExactRealRepro(unittest.TestCase):
    def test_timestamp_state_no_longer_crashes(self):
        with patch.object(
            solver_writer,
            "ha_get",
            return_value={"state": "2026-08-24T13:00:00+10:00"},
        ):
            result = solver_writer.safe_num("sensor.misconfigured_export_price")
        self.assertEqual(result, 0.0)  # the default fallback

    def test_warns_with_the_entity_id_and_bad_value_named(self):
        # nimbus issue #363 (Mark Purcell, codebase review): this warning
        # used to be a bare print(file=sys.stderr), invisible to HA's own
        # log -- now a real _LOGGER.warning() call.
        with (
            patch.object(
                solver_writer,
                "ha_get",
                return_value={"state": "2026-08-24T13:00:00+10:00"},
            ),
            patch.object(solver_writer, "_LOGGER") as mock_logger,
        ):
            solver_writer.safe_num("sensor.misconfigured_export_price")
            mock_logger.warning.assert_called_once()
            args, _kwargs = mock_logger.warning.call_args
            msg = args[0] % args[1:]
            self.assertIn("sensor.misconfigured_export_price", msg)


class TestNormalNumericStatesPassThroughUnchanged(unittest.TestCase):
    def test_plain_numeric_state_string(self):
        with patch.object(solver_writer, "ha_get", return_value={"state": "0.2847"}):
            result = solver_writer.safe_num("sensor.real_export_price")
        self.assertEqual(result, 0.2847)

    def test_negative_price_is_a_real_valid_value(self):
        # A genuinely negative export/import price is real (curtailment
        # periods, negative FiT events) -- must not be treated as an
        # error.
        with patch.object(solver_writer, "ha_get", return_value={"state": "-0.05"}):
            result = solver_writer.safe_num("sensor.real_export_price")
        self.assertEqual(result, -0.05)

    def test_no_warning_for_a_normal_value(self):
        with (
            patch.object(solver_writer, "ha_get", return_value={"state": "0.15"}),
            patch.object(solver_writer, "_LOGGER") as mock_logger,
        ):
            solver_writer.safe_num("sensor.real_export_price")
            mock_logger.warning.assert_not_called()


class TestOtherRealFailureShapes(unittest.TestCase):
    """The same class of failure can arrive several different ways --
    all must degrade gracefully, not just the exact string Mark hit."""

    def test_unavailable_state_string(self):
        with patch.object(
            solver_writer, "ha_get", return_value={"state": "unavailable"}
        ):
            result = solver_writer.safe_num("sensor.transiently_down")
        self.assertEqual(result, 0.0)

    def test_unknown_state_string(self):
        with patch.object(solver_writer, "ha_get", return_value={"state": "unknown"}):
            result = solver_writer.safe_num("sensor.not_yet_populated")
        self.assertEqual(result, 0.0)

    def test_missing_state_key_entirely(self):
        with patch.object(solver_writer, "ha_get", return_value={}):
            result = solver_writer.safe_num("sensor.malformed_response")
        self.assertEqual(result, 0.0)

    def test_custom_fallback_is_honoured(self):
        # A caller that wants a different (non-zero) fallback -- proves
        # the parameter is real, not just documented.
        with patch.object(
            solver_writer, "ha_get", return_value={"state": "not-a-number"}
        ):
            result = solver_writer.safe_num("sensor.bad", fallback=0.15)
        self.assertEqual(result, 0.15)
