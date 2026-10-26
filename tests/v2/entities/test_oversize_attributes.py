"""An attribute set too large for the recorder is named when its history is
lost (#944) (#1837).
"""

from __future__ import annotations

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


class TestTheMessageIsActionable(_Harness):
    def setUp(self):
        super().setUp()
        self.msg = self._warn(
            "sensor.nimbus_household_load_total_forecast", _payload_of(20_000)
        )[0].getMessage()


class TestItNeverBreaksAPublish(_Harness):
    def test_unserialisable_attributes_do_not_raise(self):
        """A diagnostic must never be the reason a real publish fails."""
        solver_writer._warn_if_attrs_exceed_recorder_cap(
            "sensor.weird", {"obj": object()}
        )


if __name__ == "__main__":
    unittest.main()
