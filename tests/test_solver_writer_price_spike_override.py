"""Direct test coverage for nimbus issue #567's own decision logic --
solver_writer.resolve_price_spike_override(): whether a spike is
detected (the dashboard's own real-time "$$$" visibility signal,
independent of arming) and whether the discharge override actually
becomes active (requires arming + detection + a real configured rate).

nimbus issue #694: the override now WINS over an active P2P
fixed-export commitment rather than being exempted from it -- see
TestSpikeWinsOverP2P below (this file previously had the exemption
tested as TestP2PExemption, flipped here to match the household's own
explicit reversal of #567's original priority).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _cfg(**overrides):
    cfg = {
        "solver_price_spike_threshold": 0.0,
        "solver_price_spike_discharge_kw": 0.0,
        "solver_price_spike_override_armed": False,
    }
    cfg.update(overrides)
    return cfg


class TestSpikeDetectionByThreshold(unittest.TestCase):
    def test_price_at_or_above_threshold_is_detected(self):
        cfg = _cfg(solver_price_spike_threshold=1.0)
        _kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.0)
        self.assertTrue(detected)

    def test_price_below_threshold_is_not_detected(self):
        cfg = _cfg(solver_price_spike_threshold=1.0)
        _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.99)
        self.assertFalse(detected)

    def test_zero_threshold_never_fires_on_its_own(self):
        """0 (the default) means "not configured" -- must not be
        interpreted as "any positive price is a spike"."""
        cfg = _cfg(solver_price_spike_threshold=0.0)
        _kw, detected = solver_writer.resolve_price_spike_override(cfg, 5.0)
        self.assertFalse(detected)


class TestSpikeDetectionByAlertEntity(unittest.TestCase):
    def test_alert_entity_on_is_detected_even_below_threshold(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_alert_entity="binary_sensor.retailer_spike",
        )
        with patch.object(
            solver_writer, "ha_get", return_value={"state": "on"}
        ) as mock_get:
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        mock_get.assert_called_once_with("binary_sensor.retailer_spike")
        self.assertTrue(detected)

    def test_alert_entity_off_is_not_detected(self):
        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(solver_writer, "ha_get", return_value={"state": "off"}):
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)

    def test_alert_entity_fetch_failure_fails_open_to_not_detected(self):
        """A spike-alert integration having a bad moment must never
        crash a regular solve cycle -- fails open to "no alert", same
        posture as every other optional external entity read."""
        import urllib.error

        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(
            solver_writer,
            "ha_get",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        self.assertFalse(detected)

    def test_no_alert_entity_configured_is_a_clean_no_op(self):
        cfg = _cfg()
        with patch.object(solver_writer, "ha_get") as mock_get:
            _kw, detected = solver_writer.resolve_price_spike_override(cfg, 0.10)
        mock_get.assert_not_called()
        self.assertFalse(detected)


class TestOverrideRequiresArmingAndARealRate(unittest.TestCase):
    def test_detected_but_not_armed_gives_no_override(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=20.0,
            solver_price_spike_override_armed=False,
        )
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5)
        self.assertTrue(detected)  # visibility still fires
        self.assertIsNone(kw)  # but no override, human hasn't armed it

    def test_armed_and_detected_but_zero_rate_gives_no_override(self):
        """A rate of 0 (never explicitly set by the household) must not
        silently discharge at 0kW -- that's a real, deliberate pin
        elsewhere (network.py's own test coverage), but here it means
        "the household hasn't configured a real rate yet"."""
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=0.0,
            solver_price_spike_override_armed=True,
        )
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5)
        self.assertTrue(detected)
        self.assertIsNone(kw)

    def test_armed_detected_and_a_real_rate_gives_the_override(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
        )
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5)
        self.assertTrue(detected)
        self.assertEqual(kw, 25.0)


class TestSpikeWinsOverP2P(unittest.TestCase):
    """nimbus issue #694: household's own explicit reversal of #567's
    original P2P exemption -- "i want it to win over p2p cos p2p will
    be lower... by default.... otherwise it makes no sense". A P2P
    fixed-export commitment being configured/active must no longer
    suppress the override at all; resolve_price_spike_override() itself
    doesn't even look at P2P state any more (network.py's own
    grid_export[0] bounds are what actually release the P2P pin for
    that period -- see that module's own test coverage)."""

    def test_p2p_block_configured_no_longer_blocks_the_override(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
            solver_p2p_block_1_rate_kw=11.5,
            solver_p2p_block_1_start_hour=17,
            solver_p2p_block_1_end_hour=24,
        )
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5)
        self.assertTrue(detected)
        self.assertEqual(kw, 25.0)  # wins over P2P, not exempted

    def test_outside_any_p2p_window_the_override_still_applies(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
        )
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5)
        self.assertTrue(detected)
        self.assertEqual(kw, 25.0)


if __name__ == "__main__":
    unittest.main()
