"""nimbus #1067: a battery participant with no power limits at all joins
the fleet silently, and now says so.

`build_extra_batteries()` guards two things and logs well about both:

    if capacity_kwh <= 0.0 or not soc_sensor:
        WARNING "... missing capacity_kwh or its SoC sensor -- skipping"

`max_charge_kw` and `max_discharge_kw` are not among them. Both read
sites fall back to `0.0`, so a participant with zero of each passes every
check above — capacity is positive, the SoC sensor is present — and joins
the fleet as a battery the LP can never move, with no log line anywhere.

**Reachable without touching the schema.** The wizard requires both
fields, but `0` is a valid entry for a kW selector.

**And not inert.** `battery_oracle` still hands that participant its full
SoC envelope, so the scorer's oracle models a fleet member it can never
dispatch — moving `j_star`, and therefore EPR and regret. The same shape
as the phantom recorded on #768, reached by a different route.

## Why a WARNING rather than a `continue`

Excluding an immobile participant is probably the right end state. It is
also a change to what a live install solves, decided while the household
was unreachable, on a configuration whose live effect could not be
inspected — the dev install's own participant config is not readable
through the available tooling, so "it would only affect a broken setup"
was an assumption rather than a measurement.

So this change makes the silent case **loud** and nothing else. The
stronger version is argued on #1067 and belongs to whoever decides it.

The tests below pin that distinction deliberately: one asserts the
warning fires, another asserts the participant is *still built*. If
someone later adds the `continue`, the second fails and forces the
behaviour change to be a decision rather than a side effect.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from solver_inputs import extra_batteries as extra_batteries_inputs


def _source() -> str:
    # nimbus issue #1300: this guard moved to solver_inputs/extra_batteries.py
    # with build_extra_batteries() itself.
    path = extra_batteries_inputs.__file__.replace(".pyc", ".py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _guard_block() -> str:
    """The region from the existing capacity/SoC guard through to the
    min/max SoC reads, which is where the new check sits."""
    text = _source()
    start = text.index("if capacity_kwh <= 0.0 or not soc_sensor:")
    end = text.index(
        "min_soc_pct = float(data.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT)", start
    )
    return text[start:end]


class TestTheImmobileCaseIsDetected(unittest.TestCase):
    def test_both_power_limits_are_checked(self):
        block = _guard_block()
        self.assertIn("max_charge_kw <= 0.0", block)
        self.assertIn("max_discharge_kw <= 0.0", block)

    def test_it_requires_BOTH_to_be_zero(self):
        """A participant that can only discharge (an EV that never
        exports, or a pack whose charger is on another circuit) is a
        legitimate configuration. Only zero in *both* directions is
        undispatchable."""
        block = _guard_block()
        self.assertIn(
            "max_charge_kw <= 0.0\n            and max_discharge_kw <= 0.0",
            block,
            "the two power limits are no longer combined with `and`. "
            "With `or`, a one-directional participant -- a legitimate "
            "setup -- would be warned about on every install that has "
            "one.",
        )

    def test_the_message_says_why_it_matters(self):
        """ "has no power limits" is trivia. That the oracle still gets
        its SoC envelope, and that this moves the score, is the reason
        the line exists."""
        block = _guard_block()
        self.assertIn("oracle", block)
        self.assertIn("j_star", block)

    def test_it_tells_the_household_what_to_do(self):
        block = _guard_block()
        self.assertIn("Set real", block)
        self.assertIn("remove the participant", block)


class TestItIsLoggedOncePerParticipant(unittest.TestCase):
    """`build_extra_batteries()` runs every solve, and a condition that is
    true once is true every cycle until someone reconfigures. #945 is the
    precedent -- a WARNING firing 99 times an hour buried its own signal.
    """

    def test_there_is_a_module_level_seen_set(self):
        self.assertIsInstance(extra_batteries_inputs._IMMOBILE_PARTICIPANT_WARNED, set)

    def test_the_set_is_keyed_by_subentry_not_by_name(self):
        """Two participants can share a display name; subentry_id is the
        only stable identity."""
        block = _guard_block()
        self.assertIn("subentry.subentry_id not in _IMMOBILE_PARTICIPANT_WARNED", block)
        self.assertIn("_IMMOBILE_PARTICIPANT_WARNED.add(subentry.subentry_id)", block)


class TestNothingIsExcluded(unittest.TestCase):
    """The load-bearing half. This change is a WARNING and nothing else --
    if it ever grows a `continue`, that is a behaviour change to a live
    install's solve and must be decided rather than absorbed."""

    def test_the_immobile_branch_does_not_skip_the_participant(self):
        block = _guard_block()
        immobile = block[block.index("if (\n            max_charge_kw <= 0.0") :]
        self.assertNotIn(
            "continue",
            immobile,
            "the immobile-participant branch now skips the participant. "
            "That may well be right, but it changes what a live install "
            "solves and #1067 carries the argument -- it should land as "
            "its own deliberate change with its own note, not inside the "
            "warning that was added to make the case visible.",
        )

    def test_the_existing_capacity_guard_still_skips(self):
        """The one that always did, and should keep doing so."""
        block = _guard_block()
        capacity = block[: block.index("# nimbus issue #1067")]
        self.assertIn("continue", capacity)


if __name__ == "__main__":
    unittest.main()
