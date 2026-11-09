"""Whether a price spike is detected, and whether the discharge override
becomes active (#567) (#1837).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import solver_writer
from solver_inputs import prices as price_inputs


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
            _kw, detected = price_inputs.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)

        # test_alert_entity_fetch_failure_fails_open_to_not_detected: A spike-alert integration having a bad moment must never
        import urllib.error

        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(
            solver_writer,
            "ha_get",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            _kw, detected = price_inputs.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)


class TestTheOverrideFiresOnlyWhenArmedSeenAndSet(unittest.TestCase):
    def test_the_override_discharges_only_when_armed_a_spike_is_seen_and_a_rate_is_set(
        self,
    ):
        """#567: the household arms it; a spike alone never discharges, and an
        armed override with no rate does nothing. Detection is reported either way."""
        spike = {"solver_price_spike_threshold": 1.00}
        for overrides, price, expected in [
            (
                {
                    **spike,
                    "solver_price_spike_override_armed": True,
                    "solver_price_spike_discharge_kw": 5.0,
                },
                1.50,
                (5.0, True),
            ),
            (
                {
                    **spike,
                    "solver_price_spike_override_armed": True,
                    "solver_price_spike_discharge_kw": 5.0,
                },
                0.50,
                (None, False),
            ),
            (
                {
                    **spike,
                    "solver_price_spike_override_armed": False,
                    "solver_price_spike_discharge_kw": 5.0,
                },
                1.50,
                (None, True),
            ),
            (
                {
                    **spike,
                    "solver_price_spike_override_armed": True,
                    "solver_price_spike_discharge_kw": 0.0,
                },
                1.50,
                (None, True),
            ),
        ]:
            with self.subTest(overrides=overrides, price=price):
                self.assertEqual(
                    price_inputs.resolve_price_spike_override(_cfg(**overrides), price),
                    expected,
                )

        # The alert entity alone is a spike too, with no threshold set.
        cfg = _cfg(
            solver_price_spike_alert_entity="binary_sensor.retailer_spike",
            solver_price_spike_override_armed=True,
            solver_price_spike_discharge_kw=4.0,
        )
        with patch.object(solver_writer, "ha_get", return_value={"state": "on"}):
            self.assertEqual(
                price_inputs.resolve_price_spike_override(cfg, 0.10), (4.0, True)
            )


if __name__ == "__main__":
    unittest.main()
