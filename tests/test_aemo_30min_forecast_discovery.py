"""nimbus issue #452: discovering `sensor.aemo_nem_<region>_current_30min_forecast`.

Mark Purcell chose auto-discovery over a wizard field — *"proceed with
auto discovery"* — consistent with #495's geocoded-location discovery and
#768's power-sensor discovery, and with #449 tracking that wizard as
already too large at 26 fields.

The cases that matter are the ones a plausible implementation gets wrong,
and two of them this project has already paid for once:

- **Suffix, never a region.** `const.py` records that hardcoding one
  specific region's entity (`sensor.aemo_nem_qld1_...`) was a real bug —
  a NSW household could never reach it. Every region must resolve through
  the identical path.
- **Filter unusable states BEFORE counting.** v0.94.300 had to ship
  exactly this correction for the geocoded discovery after a live deploy
  found the one candidate reading `unavailable`. "Exactly one" has to mean
  one sensor that can actually answer, or the rule is about the wrong
  thing.
- **Refuse rather than pick.** A household running two AEMO integrations
  genuinely has two answers; choosing whichever sorts first tags every
  comparison with a silently-chosen source.

Exercised through the same logic the sensor uses, in the style
`test_nem_region.py` already established for #495's discovery half.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
from sensor_discovery import discover_aemo_30min_forecast_sensor


class _State:
    """The two attributes the discovery rule reads off an HA state."""

    def __init__(self, entity_id: str, state: str):
        self.entity_id = entity_id
        self.state = state


def _discover(entity_ids_and_states):
    """Calls the SHIPPED rule -- `sensor_discovery.
    discover_aemo_30min_forecast_sensor()`, the exact function
    `NimbusSolverConfigSensor._discover_aemo_30min_forecast_sensor()`
    delegates to.

    This used to be a hand-copied mirror of that logic, which nimbus
    issue #954 (Mark Purcell) correctly called out: the copy agreed with
    the real code, but agreement is not coverage. Any future edit to the
    shipped rule would have kept this file green while the shipped path
    quietly lost its only tests -- the "tested helper wired to nothing"
    shape this project has already been burned by twice (#538, #692).
    """
    return discover_aemo_30min_forecast_sensor(
        _State(eid, state) for eid, state in entity_ids_and_states
    )


class TestEveryRegionResolvesIdentically(unittest.TestCase):
    """The region-hardcoding bug this project already fixed once must not
    return in a new place."""

    def test_each_nem_region_is_found_the_same_way(self):
        for region in ("qld1", "nsw1", "vic1", "sa1", "tas1"):
            with self.subTest(region=region):
                eid = f"sensor.aemo_nem_{region}_current_30min_forecast"
                self.assertEqual(_discover([(eid, "-0.0085")]), eid)

    def test_no_region_string_is_required_at_all(self):
        """A differently-named integration publishing the same suffix
        still resolves — the match is on the shape of the data, not on
        anyone's naming convention."""
        eid = "sensor.some_other_aemo_integration_current_30min_forecast"
        self.assertEqual(_discover([(eid, "0.12")]), eid)


class TestExactlyOneUsableCandidate(unittest.TestCase):
    def test_one_is_used(self):
        got = _discover(
            [
                ("sensor.unrelated", "whatever"),
                ("sensor.aemo_nem_qld1_current_5min_period_price", "0.10"),
                ("sensor.aemo_nem_qld1_current_30min_forecast", "-0.0085"),
            ]
        )
        self.assertEqual(got, "sensor.aemo_nem_qld1_current_30min_forecast")

    def test_two_integrations_refuse_rather_than_pick(self):
        """Two genuine answers. Picking the first would tag every
        comparison with a silently-chosen source."""
        self.assertIsNone(
            _discover(
                [
                    ("sensor.aemo_a_current_30min_forecast", "0.10"),
                    ("sensor.aemo_b_current_30min_forecast", "0.11"),
                ]
            )
        )

    def test_none_present_is_a_clean_no_op(self):
        """A household without the integration gets nothing, not an
        error — same blank-is-off convention as every other optional
        source in this project."""
        self.assertIsNone(_discover([("sensor.unrelated", "x")]))

    def test_the_5min_sensor_is_not_mistaken_for_the_30min_one(self):
        """They come from the same integration and differ only by
        suffix; matching loosely would compare a forecast against
        itself."""
        self.assertIsNone(
            _discover([("sensor.aemo_nem_qld1_current_5min_period_price", "0.10")])
        )


class TestUnusableStatesAreFilteredFirst(unittest.TestCase):
    """v0.94.300 shipped exactly this correction for #495's discovery
    after a live deploy found the sole candidate reading `unavailable`.
    Applied here from the start rather than learned twice."""

    def test_an_unavailable_sole_candidate_yields_nothing(self):
        for bad in ("unavailable", "unknown", None):
            with self.subTest(state=bad):
                self.assertIsNone(
                    _discover([("sensor.aemo_nem_qld1_current_30min_forecast", bad)])
                )

    def test_one_unusable_does_not_block_the_other(self):
        """The edge case the filter actually fixes: two candidates where
        only one can answer must resolve from that one, not be refused as
        ambiguous."""
        got = _discover(
            [
                ("sensor.aemo_a_current_30min_forecast", "unavailable"),
                ("sensor.aemo_b_current_30min_forecast", "0.11"),
            ]
        )
        self.assertEqual(got, "sensor.aemo_b_current_30min_forecast")

    def test_filtering_happens_before_counting_not_after(self):
        """Stated as its own case because the ordering IS the fix: count
        first and this returns None (two candidates, ambiguous); filter
        first and it correctly returns the usable one."""
        entities = [
            ("sensor.aemo_a_current_30min_forecast", "unknown"),
            ("sensor.aemo_b_current_30min_forecast", "0.11"),
        ]
        self.assertIsNotNone(_discover(entities))


if __name__ == "__main__":
    unittest.main()
