"""nimbus issue #1013 -- a dashboard dial can be fully wired into the
config surface and still be read by nothing.

`number.nimbus_solver_battery_soh_percent` was created in `number.py`,
mirrored onto `sensor.nimbus_solver_config` by `sensor.py`, and consumed
by **no solve path at all** -- native or cron. A household setting State
of Health from the dashboard changed nothing about dispatch or scoring.
On the reference household it was set to 98%, in the reasonable belief
it derated a 122.2 kWh pack to ~119.8 kWh, while the solver planned
against the full 122.2. Fixed by `resolve_effective_capacity_kwh()`;
the behaviour itself is tested in
`test_effective_capacity_soh_derating.py`, this file tests the class.

**Why nimbus issue #538's guard did not catch it.** That one pairs every
`number.py` field against `sensor.py`'s live-entity resolution list, and
`solver_battery_soh_percent` IS in that list. So it verifies a value is
*published*, not that anything *consumes* it. Publication and
consumption are different properties, and only one of them makes a dial
do something.

This test checks the other half: every `solver_*` field `number.py`
offers must appear somewhere in `solver_writer.py`, which is where the
real `BatteryConfig`/`GridConfig` are built.

A swept check at the time of writing found **exactly one** inert field
out of 44, so the problem was isolated rather than systemic -- worth
recording, because "how many others are there" was the first question
and the answer was one. That one is now fixed, so the sweep should find
none; the test's value from here on is catching the next one on the day
it is added rather than years later.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

# nimbus issue #1013. The single known-inert field WAS
# `solver_battery_soh_percent`; its entry was deleted when the fix
# landed, exactly as the exemption's own text instructed, and
# `resolve_effective_capacity_kwh()` now reads it on every solve path.
#
# The dict stays, empty, because it is the mechanism rather than the
# finding: a new inert field fails `test_no_solver_number_is_silently_
# inert` immediately, and the only sanctioned way past that is to add an
# entry here with an issue number and a reason. Deliberately not a
# blanket "ignore unknown fields".
KNOWN_INERT: dict[str, str] = {}


def _solver_number_fields() -> set[str]:
    """Every `solver_*` config key `number.py` builds an entity for."""
    nums = (_ROOT / "number.py").read_text(encoding="utf-8")
    const = (_ROOT / "const.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bCONF_[A-Z0-9_]+\b", nums))
    values = dict(
        re.findall(
            r"^(CONF_[A-Z0-9_]+)\s*:\s*Final\s*=\s*\"([^\"]+)\"", const, re.MULTILINE
        )
    )
    return {
        values[c] for c in referenced if c in values and values[c].startswith("solver_")
    }


class TestEverySolverNumberIsActuallyRead(unittest.TestCase):
    def setUp(self):
        self.fields = _solver_number_fields()
        self.writer = (_ROOT / "solver_writer.py").read_text(encoding="utf-8")

    def test_the_sweep_finds_a_real_set_of_fields(self):
        """Guards the guard: a regex that silently matched nothing would
        make every assertion below pass vacuously."""
        self.assertGreater(
            len(self.fields),
            30,
            "the number.py/const.py sweep found implausibly few solver "
            "fields -- the parsing has probably drifted, and this whole "
            "file would be passing on an empty set",
        )

    def test_no_solver_number_is_silently_inert(self):
        inert = sorted(
            f for f in self.fields if f not in self.writer and f not in KNOWN_INERT
        )
        self.assertEqual(
            inert,
            [],
            "these dashboard-editable solver settings are never read by "
            "solver_writer.py, so changing them has ZERO effect on "
            f"dispatch or scoring: {inert}. This is the nimbus #538 / "
            "#1013 class -- a dial that looks live and does nothing. "
            "Either wire it into the solve, or add it to KNOWN_INERT "
            "with the issue number and a reason.",
        )

    def test_known_inert_fields_are_still_inert(self):
        """Self-cleaning. When #1013 is fixed, this fails and says so,
        rather than leaving a stale exemption to quietly protect a field
        that no longer needs protecting."""
        for field, reason in KNOWN_INERT.items():
            with self.subTest(field=field):
                self.assertNotIn(
                    field,
                    self.writer,
                    f"{field} is now read by solver_writer.py -- good. "
                    f"Delete its KNOWN_INERT entry. Recorded reason was: "
                    f"{reason}",
                )

    def test_known_inert_fields_still_exist_at_all(self):
        """A renamed or removed field must not linger as a dead
        exemption that silently weakens the check above."""
        for field in KNOWN_INERT:
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    self.fields,
                    f"{field} is in KNOWN_INERT but number.py no longer "
                    "offers it -- remove the stale entry",
                )


if __name__ == "__main__":
    unittest.main()
