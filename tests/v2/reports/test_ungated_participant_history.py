"""A battery participant scored with no away gate says so (#1247) (#1837)."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import solver_writer as sw
from custom_components.nimbus_load.solver_inputs import battery_participants as bp


class TestTheWarningExists(unittest.TestCase):
    def test_the_helper_is_defined(self):
        self.assertTrue(hasattr(bp, "_warn_participant_history_is_ungated_once"))

    def test_it_warns_once_per_participant(self):
        """Once per participant, not once globally -- a household with two
        EVs where only one is configured must still hear about the other."""
        bp._UNGATED_PARTICIPANT_WARNED.clear()
        seen: list[tuple] = []
        real = sw._LOGGER.warning
        sw._LOGGER.warning = lambda msg, *a: seen.append(a)
        try:
            bp._warn_participant_history_is_ungated_once("ev_m3p")
            bp._warn_participant_history_is_ungated_once("ev_m3p")
            bp._warn_participant_history_is_ungated_once("ev_my")
        finally:
            sw._LOGGER.warning = real
            bp._UNGATED_PARTICIPANT_WARNED.clear()
        self.assertEqual([a[0] for a in seen], ["ev_m3p", "ev_my"])


class TestItNeverGuesses(unittest.TestCase):
    """Reports, never corrects -- the #1241 line, with a sharper reason here.

    Without the entity there is genuinely no evidence of when the car was
    away. Inferring it from a power trace would invent the very fact the gate
    exists to supply, and would do so most confidently on exactly the days a
    long trip makes the trace look most like sustained discharge.
    """

    def test_it_returns_nothing(self):
        """A helper that returned a mask would invite a caller to use it."""
        sig = inspect.signature(bp._warn_participant_history_is_ungated_once)
        # A STRING, not None: solver_writer.py uses `from __future__ import
        # annotations`, so every annotation is unevaluated text.
        self.assertIn(sig.return_annotation, (None, "None"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
