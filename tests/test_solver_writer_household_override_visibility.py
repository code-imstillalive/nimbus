"""Real regression test for nimbus issue #348 (Mark Purcell, codebase
review): several real, deliberate household-specific tuning choices in
solver_writer.py (a hardcoded day/night discharge-cost schedule, a fixed
daily charge, LocalVolts-specific price-array key parsing, a hardcoded
post-midnight self-consume window, and a hardcoded P2P matched-rate hour
window) used to silently override or ignore an install's own
wizard-configured values with zero log visibility into that happening.

Three of the four findings are now real, wizard-configurable fields (see
test_solver_writer_fixed_daily_charge_and_self_consume_hours.py for the
fixed daily charge / post-midnight self-consume window, and
test_scheduled_discharge_cost_and_salvage_value.py for the day/night
discharge-cost schedule and the costsflexup/earningsflexup price-array
keys, all fixed 2026-09-04) -- none of them are silent anymore, so this
file's own coverage of them was removed rather than kept testing stale
behaviour. Only the P2P matched-rate hour window remains a genuine,
considered tradeoff not yet generalised -- this file now covers exactly
that one, plus the function's own "logs nothing for a fully generic
install" and "only once per process" contracts.

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
        """A fresh install with no P2P matched-rate sensor logs nothing
        at all -- the only remaining override in this function is
        genuinely config-gated."""
        cfg = {}
        with patch.object(solver_writer, "_LOGGER") as mock_logger:
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
