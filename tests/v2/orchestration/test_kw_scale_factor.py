"""A power sensor's history is scaled by its own declared unit before it is
treated as kW (#1837).
"""

from __future__ import annotations

from unittest.mock import patch

import solver_shared
import solver_writer

_kw_scale_factor = solver_writer._kw_scale_factor


def _mock_ha_get(unit):
    def _fn(entity_id):
        return {
            "entity_id": entity_id,
            "state": "1.0",
            "attributes": {"unit_of_measurement": unit},
        }

    return _fn


def test_unrelated_unit_defaults_to_1():
    # Only "W" is specifically corrected -- this deliberately does not
    # guess at every possible power unit HA could report, only the one
    # real, confirmed-live mismatch.
    with patch.object(solver_shared, "ha_get", _mock_ha_get("A")):
        assert _kw_scale_factor("sensor.some_current_sensor") == 1.0
