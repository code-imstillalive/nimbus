"""Real regression test for nimbus issue #348 (Mark Purcell, codebase
review): several real, deliberate household-specific tuning choices in
solver_writer.py (a hardcoded day/night discharge-cost schedule, a fixed
daily charge, LocalVolts-specific price-array key parsing, a hardcoded
post-midnight self-consume window, and a hardcoded P2P matched-rate hour
window) silently override or ignore an install's own wizard-configured
values with zero log visibility into that happening.

Each one is a genuine, considered tradeoff (documented individually at
its own definition, kept rather than generalised under time pressure to
avoid risking a real, revenue-affecting change to core LP cost logic) --
this fix is deliberately scoped to the issue's own suggested INTERIM step
only: make every currently-active override visible in the log, once per
process, by name. Making each one a real, generic wizard field (the
issue's own longer-term suggested fix) is a materially larger, riskier
change to core dispatch/cost math, left for a dedicated follow-up.

Imports and exercises the REAL _log_active_household_specific_overrides_
once() (not a reimplementation), same "import solver_writer directly"
pattern as its sibling solver_writer test files.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


class TestHouseholdOverrideVisibility(unittest.TestCase):
    def setUp(self):
        solver_writer._household_specific_overrides_logged = False

    def tearDown(self):
        solver_writer._household_specific_overrides_logged = False

    def test_logs_nothing_active_for_a_fully_generic_install(self):
        """A fresh install with no price-forecast-array sensor, no P2P
        matched-rate sensor, and no P2P block running through midnight
        still gets ONE warning for the always-on fixed daily charge --
        the other three overrides are genuinely config-gated and absent
        here."""
        cfg = {}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args[0][-1]
        self.assertIn("fixed daily charge", message)
        self.assertNotIn("costsflexup", message)
        self.assertNotIn("matched rate", message)

    def test_logs_price_forecast_array_override_when_configured_and_live(self):
        cfg = {"solver_price_forecast_array_sensor": "sensor.my_price_array"}
        with (
            patch.object(solver_writer, "entity_exists", return_value=True),
            patch.object(solver_writer, "_LOGGER") as mock_logger,
        ):
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertIn("sensor.my_price_array", message)
        self.assertIn("costsflexup", message)
        self.assertIn("IGNORED", message)

    def test_does_not_log_price_forecast_array_override_when_sensor_not_live(self):
        cfg = {"solver_price_forecast_array_sensor": "sensor.my_price_array"}
        with (
            patch.object(solver_writer, "entity_exists", return_value=False),
            patch.object(solver_writer, "_LOGGER") as mock_logger,
        ):
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertNotIn("costsflexup", message)

    def test_logs_p2p_matched_rate_override_when_sensor_configured(self):
        cfg = {"solver_p2p_matched_rate_forecast_sensor": "sensor.my_p2p_matched"}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertIn("matched rate is forced to 0", message)

    def test_logs_midnight_self_consume_override_when_a_p2p_block_runs_through_midnight(
        self,
    ):
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17.0,
            "solver_p2p_block_1_end_hour": 24.0,
        }
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertIn("runs through midnight", message)
        self.assertIn(
            str(solver_writer.SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE), message
        )

    def test_does_not_log_midnight_override_when_no_p2p_block_reaches_24(self):
        cfg = {
            "solver_p2p_block_1_rate_kw": 11.5,
            "solver_p2p_block_1_start_hour": 17.0,
            "solver_p2p_block_1_end_hour": 23.0,
        }
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertNotIn("runs through midnight", message)

    def test_only_logs_once_per_process_not_once_per_cycle(self):
        cfg = {"solver_p2p_matched_rate_forecast_sensor": "sensor.my_p2p_matched"}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)
            solver_writer._log_active_household_specific_overrides_once(cfg)
            solver_writer._log_active_household_specific_overrides_once(cfg)

        mock_logger.warning.assert_called_once()

    def test_never_raises_even_if_entity_exists_itself_raises(self):
        cfg = {"solver_price_forecast_array_sensor": "sensor.my_price_array"}
        with patch.object(
            solver_writer, "entity_exists", side_effect=RuntimeError("boom")
        ):
            solver_writer._log_active_household_specific_overrides_once(cfg)  # no raise


if __name__ == "__main__":
    unittest.main()
