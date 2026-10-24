"""An attribute set too large for the recorder is named when its history is
lost (#944) (#1837).
"""

from __future__ import annotations

import json
import logging
import unittest

import solver_writer


def _payload_of(size_bytes: int) -> dict:
    """Attributes whose JSON encoding exceeds `size_bytes`."""
    return {"forecast": ["x" * 64] * (size_bytes // 64), "unit_of_measurement": "kW"}


class _Harness(unittest.TestCase):
    def setUp(self):
        solver_writer._OVERSIZE_ATTRS_WARNED.clear()

    def tearDown(self):
        solver_writer._OVERSIZE_ATTRS_WARNED.clear()

    def _warn(self, entity_id, attributes):
        with self.assertLogs(solver_writer._LOGGER, level=logging.DEBUG) as captured:
            solver_writer._LOGGER.debug("anchor")
            solver_writer._warn_if_attrs_exceed_recorder_cap(entity_id, attributes)
        return [r for r in captured.records if "#944" in r.getMessage()]


class TestItFiresOnlyWhenHistoryIsActuallyLost(_Harness):
    def test_an_oversize_payload_warns(self):
        records = self._warn("sensor.big", _payload_of(20_000))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.WARNING)

    def test_a_payload_under_the_cap_is_silent(self):
        """The common case. Most Nimbus entities are well under it, and
        they must not gain a warning."""
        self.assertEqual(self._warn("sensor.small", {"a": 1, "b": "two"}), [])

    def test_empty_attributes_are_silent(self):
        self.assertEqual(self._warn("sensor.none", {}), [])

    def test_a_payload_just_under_the_cap_is_silent(self):
        """Pins the boundary against an off-by-one that would warn on
        every healthy large-but-legal payload."""
        attrs = {"pad": "x" * (solver_writer._MAX_STATE_ATTRS_BYTES - 200)}
        self.assertLessEqual(
            len(json.dumps(attrs).encode("utf-8")),
            solver_writer._MAX_STATE_ATTRS_BYTES,
        )
        self.assertEqual(self._warn("sensor.edge", attrs), [])


class TestTheMessageIsActionable(_Harness):
    def setUp(self):
        super().setUp()
        self.msg = self._warn(
            "sensor.nimbus_household_load_total_forecast", _payload_of(20_000)
        )[0].getMessage()

    def test_it_names_the_entity(self):
        self.assertIn("sensor.nimbus_household_load_total_forecast", self.msg)

    def test_it_names_the_consequence_not_just_the_size(self):
        """A size alone reads as a performance note -- which is exactly
        the existing recorder warning this exists to improve on."""
        self.assertIn("long-term statistics", self.msg)
        self.assertIn("ALL attributes", self.msg)

    def test_it_names_the_biggest_contributor(self):
        """So the reader knows which attribute to act on rather than
        having to measure the payload themselves."""
        self.assertIn("forecast=", self.msg)

    def test_it_says_the_live_state_is_fine(self):
        """Without this a reader reasonably assumes dispatch is broken.
        Only history is lost."""
        self.assertIn("live state is unaffected", self.msg)


class TestItStaysQuietAfterTheFirstTime(_Harness):
    def test_the_same_entity_warns_once_per_run(self):
        """The condition is structural -- over the cap this cycle means
        over it every cycle -- so repeating it once a minute would be the
        #757-class noise this project has already had to clean up."""
        first = self._warn("sensor.repeat", _payload_of(20_000))
        second = self._warn("sensor.repeat", _payload_of(20_000))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_a_different_entity_still_warns(self):
        """Deduplication must be per entity, not global -- both flagship
        sensors are affected and silencing the second would hide half the
        problem."""
        self._warn("sensor.first", _payload_of(20_000))
        self.assertEqual(len(self._warn("sensor.second", _payload_of(20_000))), 1)


class TestItNeverBreaksAPublish(_Harness):
    def test_unserialisable_attributes_do_not_raise(self):
        """A diagnostic must never be the reason a real publish fails."""
        solver_writer._warn_if_attrs_exceed_recorder_cap(
            "sensor.weird", {"obj": object()}
        )


if __name__ == "__main__":
    unittest.main()
