"""nimbus issue #495: resolving `region` and `postcode_prefix` from the
Companion App's reverse-geocoded address.

Both are in the telemetry schema's top-level `required` array and
neither existed anywhere in Nimbus. Mark Purcell's answer, after
checking what was already installed rather than proposing a new
mechanism: *"Use companion app reverse geocode as a one time setup."*

These are pure functions over a string, so they are tested directly.
The cases that matter are the ones a naive implementation gets wrong:

- **ACT -> NSW1.** The ACT has no NEM region of its own. A lookup table
  built from the obvious five enum values would silently mis-tag every
  record from a Canberra household.
- **WA and NT -> None.** Not in the NEM at all. A nearest-guess region
  is worse than a missing one in a feed keyed on it.
- **Last match, not first.** A suburb can legitimately contain a state
  abbreviation followed by digits; the real pair is always trailing.
- **Malformed postcode -> None, not a truncation.** A 3-digit prefix
  derived from garbage would satisfy the schema's `^[0-9]{3}$` while
  being wrong, which is the failure worth refusing.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from nem_region import (
    nem_region_for_state,
    parse_state_and_postcode,
    postcode_prefix,
    resolve_region_and_prefix,
)


class TestTheRealCompanionAppShape(unittest.TestCase):
    """The address shape Mark confirmed live on his own install --
    `"..., <STATE> <POSTCODE>, Australia"` (his real address redacted in
    the issue, so this uses the documented shape with a stand-in)."""

    ADDRESS = "12 Example Street, Noosa Heads QLD 4567, Australia"

    def test_region_and_prefix_together(self):
        self.assertEqual(resolve_region_and_prefix(self.ADDRESS), ("QLD1", "456"))

    def test_raw_pair(self):
        self.assertEqual(parse_state_and_postcode(self.ADDRESS), ("QLD", "4567"))

    def test_a_longer_address_with_a_unit_number(self):
        addr = "Unit 3/12 Example Street, Some Suburb, VIC 3000, Australia"
        self.assertEqual(resolve_region_and_prefix(addr), ("VIC1", "300"))

    def test_a_short_address_with_no_street(self):
        self.assertEqual(resolve_region_and_prefix("Hobart TAS 7000"), ("TAS1", "700"))


class TestEveryNemRegion(unittest.TestCase):
    def test_the_five_schema_enum_values_are_all_reachable(self):
        """The schema's enum is exactly these five -- each must be
        produced by at least one real state."""
        got = {nem_region_for_state(s) for s in ("NSW", "QLD", "VIC", "SA", "TAS")}
        self.assertEqual(got, {"NSW1", "QLD1", "VIC1", "SA1", "TAS1"})

    def test_act_maps_to_nsw1(self):
        """The one non-obvious entry. The ACT sits inside NSW1 and has no
        region of its own; a table built from the enum alone would miss
        it and mis-tag every Canberra record."""
        self.assertEqual(nem_region_for_state("ACT"), "NSW1")
        self.assertEqual(
            resolve_region_and_prefix("Canberra ACT 2600, Australia"),
            ("NSW1", "260"),
        )

    def test_lowercase_state_is_accepted(self):
        self.assertEqual(nem_region_for_state("qld"), "QLD1")
        self.assertEqual(
            resolve_region_and_prefix("somewhere qld 4000"), ("QLD1", "400")
        )


class TestNonNemStates(unittest.TestCase):
    """WA and NT are not in the NEM. Returning a nearest guess would be
    worse than returning nothing in a telemetry feed keyed on region."""

    def test_wa_has_no_region_but_still_has_a_prefix(self):
        region, prefix = resolve_region_and_prefix("Perth WA 6000, Australia")
        self.assertIsNone(region)
        self.assertEqual(prefix, "600")

    def test_nt_has_no_region_but_still_has_a_prefix(self):
        region, prefix = resolve_region_and_prefix("Darwin NT 0800, Australia")
        self.assertIsNone(region)
        self.assertEqual(prefix, "080")

    def test_the_two_fields_fail_independently(self):
        """They fail for genuinely different reasons, so one being
        unavailable must not suppress the other."""
        region, prefix = resolve_region_and_prefix("Perth WA 6000")
        self.assertIsNone(region)
        self.assertIsNotNone(prefix)


class TestTrailingPairWins(unittest.TestCase):
    def test_a_suburb_containing_a_state_abbreviation_does_not_win(self):
        """'Victoria SA 5000' as a suburb inside a longer NSW address --
        a first-match implementation returns SA1 here and is wrong."""
        addr = "1 Victoria SA 5000 Road, Sydney NSW 2000, Australia"
        self.assertEqual(resolve_region_and_prefix(addr), ("NSW1", "200"))


class TestMalformedInput(unittest.TestCase):
    def test_none_and_empty(self):
        for bad in (None, ""):
            with self.subTest(value=bad):
                self.assertEqual(resolve_region_and_prefix(bad), (None, None))
                self.assertEqual(parse_state_and_postcode(bad), (None, None))

    def test_an_address_with_no_state_postcode_pair(self):
        self.assertEqual(
            resolve_region_and_prefix("Somewhere, Australia"), (None, None)
        )

    def test_a_non_australian_address(self):
        self.assertEqual(
            resolve_region_and_prefix("10 Downing Street, London, UK"), (None, None)
        )

    def test_a_three_digit_postcode_is_refused_not_truncated(self):
        """Refusing beats truncating: a prefix derived from a malformed
        postcode satisfies the schema pattern while being wrong."""
        self.assertIsNone(postcode_prefix("400"))
        self.assertIsNone(postcode_prefix("40000"))
        self.assertIsNone(postcode_prefix("abcd"))
        self.assertIsNone(postcode_prefix(None))

    def test_a_five_digit_number_does_not_parse_as_a_postcode(self):
        """A US-style ZIP after a state-like token must not be accepted
        as an Australian postcode."""
        self.assertEqual(parse_state_and_postcode("Somewhere SA 50001"), (None, None))

    def test_state_abbreviation_inside_a_word_is_not_matched(self):
        """Word-boundary anchored -- 'WASHINGTON 6000' must not read as
        WA + 6000."""
        self.assertEqual(parse_state_and_postcode("WASHINGTON 6000"), (None, None))


if __name__ == "__main__":
    unittest.main()


class TestDiscoveryRefusesAmbiguity(unittest.TestCase):
    """The discovery half, exercised through the same logic the sensor
    uses: exactly one `_geocoded_location` sensor is used, several are
    refused.

    Two registered phones mean two such sensors, and they can genuinely
    disagree (two people in two places). Picking one would silently tag
    every telemetry record with whichever happened to sort first --
    the same confidently-wrong shape #768's power-sensor discovery
    refuses, and the same reason it refuses it.
    """

    @staticmethod
    def _pick(entity_ids_and_states):
        candidates = [
            (eid, state)
            for eid, state in entity_ids_and_states
            if eid.endswith("_geocoded_location")
        ]
        if len(candidates) != 1:
            return None, None
        return resolve_region_and_prefix(candidates[0][1])

    def test_exactly_one_is_used(self):
        got = self._pick(
            [
                ("sensor.unrelated", "whatever"),
                ("sensor.pixel_geocoded_location", "Noosa Heads QLD 4567, Australia"),
            ]
        )
        self.assertEqual(got, ("QLD1", "456"))

    def test_two_phones_refuse_rather_than_pick(self):
        got = self._pick(
            [
                ("sensor.pixel_geocoded_location", "Sydney NSW 2000, Australia"),
                ("sensor.iphone_geocoded_location", "Melbourne VIC 3000, Australia"),
            ]
        )
        self.assertEqual(got, (None, None))

    def test_none_present_is_a_clean_no_op(self):
        """A household without the Companion App installed gets nothing,
        not an error -- same blank-is-off convention as every other
        optional source in this project."""
        self.assertEqual(self._pick([("sensor.unrelated", "x")]), (None, None))
