"""nimbus #1131: the home battery and a battery participant default to
OPPOSITE power-sign conventions, and the asymmetry must stay visible.

The same semantic field — "does a positive reading on this power sensor
mean charging?" — is declared two different ways:

    home battery   flows/hub_options.py
                   vol.Optional, no default -> resolves falsy
                   -> positive means DISCHARGE
                   five-line comment naming #299, the vendor, and the
                   backward-compatibility reasoning

    participant    flows/battery_participant_subentry.py
                   vol.Required, default=True
                   -> positive means CHARGE
                   no comment at all, until #1131 added one

So a household accepting what both forms offer ends up with opposite
conventions for its home battery and its EV.

**This file does not assert that either default is wrong.** The
participant default may well be right — an EV charger sensor plausibly
reports positive while power flows into the car, where an inverter
reports positive while discharging. What these tests pin is that the
asymmetry cannot go back to being *invisible*, because the failure mode
is silent:

- it does not error, it inverts the charge/discharge split in the
  scorer's own history reconstruction (`solver_writer.py`'s
  `sign = -1.0 if ... else 1.0`);
- that is the #535 / #843 class — convention errors producing
  plausible-looking numbers rather than failures, both of which cost real
  investigations;
- and the resulting energy balance trips #1073 / #1098's guards, so the
  symptom looks like one of the *known confounds* rather than a
  configuration error.

If someone later establishes why the defaults differ, or aligns them, the
right move is to update the comment or drop the tests deliberately — not
to let the explanation quietly disappear again.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_FLOWS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "flows"
)
_PARTICIPANT = _FLOWS / "battery_participant_subentry.py"
_HUB = _FLOWS / "hub_options.py"


def _read(path: Path) -> str:
    """Context-managed deliberately: a bare `open(...).read()` leaks the
    handle, and pytest escalates the resulting ResourceWarning into a
    PytestUnraisableExceptionWarning that fails every test in the file
    for a reason unrelated to what they assert. This repo has paid for
    that once already."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestTheAsymmetryIsStillReal(unittest.TestCase):
    """If this ever fails, the two sides have been aligned — which is a
    fine outcome, and means these tests and the comment should be
    retired deliberately rather than left asserting a difference that no
    longer exists."""

    def test_the_home_battery_field_is_optional_with_no_default(self):
        source = _read(_HUB)
        match = re.search(
            r"vol\.Optional\(\s*CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE",
            source,
        )
        self.assertIsNotNone(
            match,
            "the home battery's power-sign field is no longer "
            "vol.Optional. If it was made Required, or given a default, "
            "the asymmetry #1131 describes has changed shape and both "
            "the comment on the participant side and these tests need "
            "revisiting.",
        )
        self.assertNotIn(
            "vol.Optional(\n                CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,\n                default=",
            source,
            "the home battery's field gained a default, so it no longer "
            "resolves falsy by omission",
        )

    def test_the_participant_field_is_required_and_defaults_true(self):
        source = _read(_PARTICIPANT)
        self.assertRegex(
            source,
            r"vol\.Required\(\s*CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE",
            "the participant's power-sign field is no longer vol.Required",
        )
        self.assertIn(
            "CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE, True",
            source,
            "the participant's power-sign default is no longer True. If "
            "it was aligned with the home battery's falsy default that "
            "is a real behaviour change for new installs and wants its "
            "own note, not a silent flip.",
        )


class TestTheAsymmetryIsDocumented(unittest.TestCase):
    """The whole of #1131's first resolution. The defaults differing is
    defensible; differing *silently* is what cost the reading."""

    def _comment_block(self) -> str:
        source = _read(_PARTICIPANT)
        end = source.index(
            "vol.Required(\n            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE"
        )
        start = source.rindex("# nimbus issue #1131", 0, end)
        return source[start:end]

    def test_the_declaration_carries_a_comment(self):
        self.assertIn("nimbus issue #1131", _read(_PARTICIPANT))

    def test_it_says_the_default_is_opposite(self):
        self.assertIn("OPPOSITE", self._comment_block())

    def test_it_names_the_counterpart_explicitly(self):
        block = self._comment_block()
        self.assertIn("CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE", block)
        self.assertIn("hub_options.py", block)

    def test_it_does_not_invent_a_justification(self):
        """The honest half. A comment asserting a reason nobody recorded
        would be worse than the silence it replaces — it would read as
        evidence."""
        block = self._comment_block()
        self.assertIn("not established", block)
        self.assertIn("No claim either way", block)

    def test_it_names_the_cost_of_getting_it_wrong(self):
        """Without this the comment is trivia. The point is that the
        failure is silent and mimics a known confound."""
        block = self._comment_block()
        self.assertIn("does\n        # not error", block)
        self.assertIn("#535", block)
        self.assertIn("#843", block)
        self.assertIn("#1073", block)


if __name__ == "__main__":
    unittest.main()
