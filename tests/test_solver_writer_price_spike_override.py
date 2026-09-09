"""Direct test coverage for nimbus issue #567's own decision logic --
solver_writer.resolve_price_spike_override(): whether a spike is
detected (the dashboard's own real-time "$$$" visibility signal,
independent of arming) and whether the discharge override actually
becomes active (requires arming + detection + a real configured rate +
NOT being inside an active P2P fixed-export commitment).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


def _grid_times(start: datetime, n: int, step_minutes: int = 5) -> list[datetime]:
    return [start + timedelta(minutes=step_minutes * i) for i in range(n)]


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
        _kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 1.0, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
        self.assertTrue(detected)

    def test_price_below_threshold_is_not_detected(self):
        cfg = _cfg(solver_price_spike_threshold=1.0)
        _kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 0.99, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
        self.assertFalse(detected)

    def test_zero_threshold_never_fires_on_its_own(self):
        """0 (the default) means "not configured" -- must not be
        interpreted as "any positive price is a spike"."""
        cfg = _cfg(solver_price_spike_threshold=0.0)
        _kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 5.0, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
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
            _kw, detected = solver_writer.resolve_price_spike_override(
                cfg, 0.10, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
            )
        mock_get.assert_called_once_with("binary_sensor.retailer_spike")
        self.assertTrue(detected)

    def test_alert_entity_off_is_not_detected(self):
        cfg = _cfg(solver_price_spike_alert_entity="binary_sensor.retailer_spike")
        with patch.object(solver_writer, "ha_get", return_value={"state": "off"}):
            _kw, detected = solver_writer.resolve_price_spike_override(
                cfg, 0.10, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
            )
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
            _kw, detected = solver_writer.resolve_price_spike_override(
                cfg, 0.10, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
            )
        self.assertFalse(detected)

    def test_no_alert_entity_configured_is_a_clean_no_op(self):
        cfg = _cfg()
        with patch.object(solver_writer, "ha_get") as mock_get:
            _kw, detected = solver_writer.resolve_price_spike_override(
                cfg, 0.10, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
            )
        mock_get.assert_not_called()
        self.assertFalse(detected)


class TestOverrideRequiresArmingAndARealRate(unittest.TestCase):
    def test_detected_but_not_armed_gives_no_override(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=20.0,
            solver_price_spike_override_armed=False,
        )
        kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 1.5, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
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
        kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 1.5, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
        self.assertTrue(detected)
        self.assertIsNone(kw)

    def test_armed_detected_and_a_real_rate_gives_the_override(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
        )
        kw, detected = solver_writer.resolve_price_spike_override(
            cfg, 1.5, _grid_times(datetime(2026, 9, 10, 12, 0, tzinfo=BRISBANE), 4)
        )
        self.assertTrue(detected)
        self.assertEqual(kw, 25.0)


class TestP2PExemption(unittest.TestCase):
    def test_active_p2p_commitment_at_period_0_blocks_the_override(self):
        """The household's own explicit scope decision -- P2P's
        "consistency of delivery is itself part of what earns the rate"
        takes priority over a spike response."""
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
            solver_p2p_block_1_rate_kw=11.5,
            solver_p2p_block_1_start_hour=17,
            solver_p2p_block_1_end_hour=24,
        )
        grid_times = _grid_times(datetime(2026, 9, 10, 17, 30, tzinfo=BRISBANE), 4)
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5, grid_times)
        self.assertTrue(detected)  # still visible on the dashboard
        self.assertIsNone(kw)  # but the override itself is exempted

    def test_outside_the_p2p_window_the_override_still_applies(self):
        cfg = _cfg(
            solver_price_spike_threshold=1.0,
            solver_price_spike_discharge_kw=25.0,
            solver_price_spike_override_armed=True,
            solver_p2p_block_1_rate_kw=11.5,
            solver_p2p_block_1_start_hour=17,
            solver_p2p_block_1_end_hour=24,
        )
        # 14:00 -- well outside the 17-24 P2P block.
        grid_times = _grid_times(datetime(2026, 9, 10, 14, 0, tzinfo=BRISBANE), 4)
        kw, detected = solver_writer.resolve_price_spike_override(cfg, 1.5, grid_times)
        self.assertTrue(detected)
        self.assertEqual(kw, 25.0)


if __name__ == "__main__":
    unittest.main()
