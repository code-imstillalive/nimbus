"""Whether a price spike is detected, and whether the discharge override
becomes active (#567) (#1837).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import solver_writer


def _cfg(**overrides):
    cfg = {
        "solver_price_spike_threshold": 0.0,
        "solver_price_spike_discharge_kw": 0.0,
        "solver_price_spike_override_armed": False,
    }
    cfg.update(overrides)
    return cfg


class TestSpikeDetectionByAlertEntity(unittest.TestCase):
    def test_alert_entity_off_is_not_detected(self):
        # test_alert_entity_off_is_not_detected
        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(solver_writer, "ha_get", return_value={"state": "off"}):
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)

        # test_alert_entity_fetch_failure_fails_open_to_not_detected: A spike-alert integration having a bad moment must never
        import urllib.error

        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(
            solver_writer,
            "ha_get",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)


if __name__ == "__main__":
    unittest.main()
