"""nimbus #1247: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in
tests/v2/reports/test_ungated_participant_history.py."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver_inputs import battery_participants as bp


class TestTheWarningExists(unittest.TestCase):
    def test_the_message_names_the_consequence_not_just_the_condition(self):
        """ "No availability entity configured" is a fact about config. What a
        household needs is what it COSTS -- otherwise the warning is noise
        they will learn to scroll past."""
        src = inspect.getsource(bp._warn_participant_history_is_ungated_once)
        lowered = src.lower()
        for phrase in ("driving", "grid connection", "regret", "epr"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_it_says_how_to_fix_it(self):
        src = inspect.getsource(bp._warn_participant_history_is_ungated_once)
        self.assertIn("battery_participant_available_entity", src)


class TestItIsWiredWhereTheGateIsSkipped(unittest.TestCase):
    def test_the_history_resolver_calls_it(self):
        src = inspect.getsource(bp._resolve_battery_participant_history)
        self.assertIn("_warn_participant_history_is_ungated_once", src)

    def test_it_fires_on_the_ABSENT_branch_not_the_present_one(self):
        """The direction matters: warning when the gate IS configured would
        be exactly backwards, and would fire on every healthy install."""
        src = inspect.getsource(bp._resolve_battery_participant_history)
        i = src.index("_warn_participant_history_is_ungated_once")
        window = src[max(0, i - 300) : i]
        self.assertIn("if not available_entity:", window)

    def test_the_real_gate_still_runs_when_configured(self):
        """This change must not disturb the gate itself -- it only adds a
        branch for the case where there isn't one."""
        src = inspect.getsource(bp._resolve_battery_participant_history)
        self.assertIn("if available_entity:", src)


class TestItNeverGuesses(unittest.TestCase):
    """Reports, never corrects -- the #1241 line, with a sharper reason here.

    Without the entity there is genuinely no evidence of when the car was
    away. Inferring it from a power trace would invent the very fact the gate
    exists to supply, and would do so most confidently on exactly the days a
    long trip makes the trace look most like sustained discharge.
    """

    def test_the_helper_does_not_synthesise_an_away_mask(self):
        src = inspect.getsource(bp._warn_participant_history_is_ungated_once)
        body = src.split('"""')[-1]
        for forbidden in ("away_period_indices", "frozenset", "infer", "= 0.0"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)
