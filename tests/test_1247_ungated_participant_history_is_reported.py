"""nimbus issue #1247: a battery participant scored with no away-gate says so.

## The gap

#768 established the mechanism and it works: a pack-power sensor also measures
real **propulsion** discharge while driving — energy that never touches the
home's grid connection. `_resolve_battery_participant_history()` gates that out
using `battery_participant_available_entity`, read as history for the scored
day, zeroing both charge and discharge for periods the car was away. #467
extended the same mask to the oracle side, so both halves of the comparison are
gated.

But the gate is:

```python
available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
if available_entity:
    ...
```

**A participant with none configured is scored ungated, silently.** A trip's
worth of discharge is priced as if it flowed through the grid, `j_ach` and EPR
absorb it, and the oracle is compared against a discharge that was really a car
leaving.

That is the #535 / #843 class — a plausible number rather than a failure.
Nothing in the published report distinguishes *"the gate ran and found the car
home"* from *"there was no gate"*, which is the whole problem.

## Where it came from

@purcell-lab's 26 Sep dispatch report (#1247) observed regret concentrated at
11:00–13:00, with the fleet discharging hard while the oracle barely did, and
said:

> this reads more like an EV departing... but that isn't verifiable from this
> data alone.

An unconfigured availability entity produces exactly that shape. Whether it is
the explanation on his install depends on his configuration, which I cannot
see and am not guessing at — but an install in that state should not have to
infer it from a regret histogram.

## Reports, never corrects

The same line #1241 draws, and here there is a sharper reason for it: without
the entity there is genuinely **no evidence** of when the car was away.
Inferring it from a power trace would invent the very fact the gate exists to
supply. `TestItNeverGuesses` pins that.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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

    def test_it_returns_nothing(self):
        """A helper that returned a mask would invite a caller to use it."""
        sig = inspect.signature(bp._warn_participant_history_is_ungated_once)
        # A STRING, not None: solver_writer.py uses `from __future__ import
        # annotations`, so every annotation is unevaluated text.
        self.assertIn(sig.return_annotation, (None, "None"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
