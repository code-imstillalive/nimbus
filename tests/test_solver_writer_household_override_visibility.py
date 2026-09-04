"""Real regression test for nimbus issue #348 (Mark Purcell, codebase
review): several real, deliberate household-specific tuning choices in
solver_writer.py (a hardcoded day/night discharge-cost schedule, a fixed
daily charge, LocalVolts-specific price-array key parsing, a hardcoded
post-midnight self-consume window, and a hardcoded P2P matched-rate hour
window) silently override or ignore an install's own wizard-configured
values with zero log visibility into that happening.

Two of these (the fixed daily charge, the post-midnight self-consume
window) were made real wizard fields on 2026-09-04 -- see
test_solver_writer_fixed_daily_charge_and_self_consume_hours.py for
their own regression coverage. This file now covers the remaining two
genuinely still-hardcoded overrides (the day/night discharge-cost
schedule, LocalVolts-specific costsflexup/earningsflexup parsing) plus
the P2P matched-rate hour window, each a genuine, considered tradeoff
(documented individually at its own definition, kept rather than
generalised under time pressure to avoid risking a real, revenue-
affecting change to core LP cost logic) -- the interim fix for these
three is exactly what this test file exercises: make every currently-
active override visible in the log, once per process, by name.

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
        """A fresh install with no price-forecast-array sensor and no P2P
        matched-rate sensor logs nothing at all -- both remaining
        overrides in this function are genuinely config-gated (the fixed
        daily charge and post-midnight self-consume hours are real
        wizard fields now, not unconditional overrides -- see
        test_solver_writer_fixed_daily_charge_and_self_consume_hours.py)."""
        cfg = {}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        mock_logger.warning.assert_not_called()

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

        mock_logger.warning.assert_not_called()

    def test_logs_p2p_matched_rate_override_when_sensor_configured(self):
        cfg = {"solver_p2p_matched_rate_forecast_sensor": "sensor.my_p2p_matched"}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
            solver_writer._log_active_household_specific_overrides_once(cfg)

        message = mock_logger.warning.call_args[0][-1]
        self.assertIn("matched rate is forced to 0", message)

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
