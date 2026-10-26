"""Discovering the household region's AEMO 30-minute forecast sensor (#452)
(#1837).
"""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
