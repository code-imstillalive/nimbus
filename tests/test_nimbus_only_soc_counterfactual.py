"""Tests for the generic, wizard-config-driven Nimbus-only counterfactual
SoC replay (2026-08-25, direct ask: "nuc one nimbus solver view has
counterfactual table.... i want u to build that into devbox package") --
compute_nimbus_only_soc_counterfactual()/publish_nimbus_only_soc_
counterfactual() (solver_writer.py), a from-scratch generalization of the
household-specific reference script
(docs/real-world-integration/files/nimbus_counterfactual_writer.py).

Direct correction applied while building this (2026-08-25): "nimbus is
written for localvolts and people without localvolts... so p2p is a
feature but also something people can ignore.. needs to be wrapped that
way" -- every P2P-related field is optional wizard config here, never a
hardcoded household constant, and a household with none of it configured
must get a complete, honest no-op (checkpoint_hour=None, viable=None),
not a crash or a leaked default.
"""

import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_battery_soc_sensor": "sensor.real_soc",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
    }
    cfg.update(overrides)
    return cfg


def _flat_history(value, day_start, day_end, step_minutes=15):
    out = []
    t = day_start
    while t < day_end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


DAY = datetime(2026, 8, 24, tzinfo=BRISBANE)
DAY_START = DAY
DAY_END = DAY_START + timedelta(days=1)


class TestGating(unittest.TestCase):
    def test_missing_solar_sensor_returns_none_without_fetching_anything(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)
        self.assertIsNone(result)
        fetch.assert_not_called()

    def test_missing_load_sensor_returns_none(self):
        cfg = _cfg(solver_whole_house_cross_check_sensor=None)
        self.assertIsNone(
            solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)
        )

    def test_missing_soc_sensor_returns_none(self):
        cfg = _cfg(solver_battery_soc_sensor=None)
        self.assertIsNone(
            solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)
        )

    def test_zero_capacity_returns_none(self):
        cfg = _cfg(solver_battery_capacity_kwh=0.0)
        self.assertIsNone(
            solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)
        )

    def test_empty_history_for_any_required_signal_returns_none(self):
        cfg = _cfg()

        def fake_fetch(entity_id, start, end):
            if entity_id == "sensor.real_solar":
                return []
            return _flat_history(1.0, start, end)

        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=fake_fetch
        ):
            self.assertIsNone(
                solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)
            )


class TestNoP2PConfigured(unittest.TestCase):
    """The majority-case household: no P2P/community-trading scheme at
    all. Must be a complete, honest no-op on every P2P-shaped field --
    never a crash, never a leaked default threshold."""

    def _fake_fetch(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(2.0, start, end)
        if entity_id == "sensor.real_soc":
            # Anchor at 50% for the whole lookback+day window.
            return _flat_history(50.0, start, end)
        if entity_id == "sensor.import_price":
            return _flat_history(0.20, start, end)
        if entity_id == "sensor.export_price":
            return _flat_history(0.05, start, end)
        raise AssertionError(f"unexpected entity fetched: {entity_id}")

    def test_real_replay_produces_a_sane_result_with_no_p2p_fields(self):
        cfg = _cfg()
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fake_fetch,
        ):
            result = solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)

        self.assertIsNotNone(result)
        self.assertEqual(result["date"], "2026-08-24")
        self.assertAlmostEqual(result["real_soc_anchor_pct"], 50.0, places=0)
        # No P2P block configured anywhere -- checkpoint/viability must be
        # a genuine no-op, not a leaked default.
        self.assertIsNone(result["checkpoint_hour"])
        self.assertIsNone(result["nimbus_only_soc_checkpoint_pct"])
        self.assertIsNone(result["real_soc_checkpoint_pct"])
        self.assertIsNone(result["viable_threshold_pct"])
        self.assertFalse(result["viable"])
        self.assertFalse(result["p2p_configured"])
        # A real, in-range close SoC -- proves the rolling replay actually
        # ran 96 real LP solves and landed somewhere physically valid.
        self.assertGreaterEqual(result["nimbus_only_soc_close_pct"], 5.0)
        self.assertLessEqual(result["nimbus_only_soc_close_pct"], 100.0)

    def test_efficiency_is_sqrt_split_not_applied_directly(self):
        # Nimbus issue #168 (Mark Purcell, 2026-08-25) -- same convention
        # fix as compute_daily_quality_report()'s own regression test:
        # solver_efficiency_percent is a round-trip figure and must be
        # sqrt()-split before use, matching main()'s own real live plan,
        # not applied directly to both directions.
        cfg = _cfg(solver_efficiency_percent=90.0)
        captured = []
        real_battery_config = solver_writer.elements.BatteryConfig

        def spy(**kwargs):
            captured.append(kwargs)
            return real_battery_config(**kwargs)

        with (
            patch.object(
                solver_writer,
                "fetch_entity_history_range",
                side_effect=self._fake_fetch,
            ),
            patch.object(solver_writer.elements, "BatteryConfig", side_effect=spy),
        ):
            solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)

        self.assertTrue(captured)
        expected = 90.0**0.5 / 10
        for kwargs in captured:
            self.assertAlmostEqual(kwargs["charge_efficiency"], expected, places=6)
            self.assertAlmostEqual(kwargs["discharge_efficiency"], expected, places=6)


class TestP2PConfigured(unittest.TestCase):
    """A household WITH a P2P block configured -- checkpoint/viability
    fields must actually populate, generically driven by whatever hour
    range that household's own wizard fields say, never a hardcoded 17."""

    def _fake_fetch(self, entity_id, start, end):
        if entity_id == "sensor.real_solar":
            return _flat_history(0.0, start, end)
        if entity_id == "sensor.real_load":
            return _flat_history(1.0, start, end)
        if entity_id == "sensor.real_soc":
            return _flat_history(80.0, start, end)
        if entity_id == "sensor.import_price":
            return _flat_history(0.20, start, end)
        if entity_id == "sensor.export_price":
            return _flat_history(0.05, start, end)
        raise AssertionError(f"unexpected entity fetched: {entity_id}")

    def test_configured_p2p_block_populates_checkpoint_fields(self):
        cfg = _cfg(
            solver_p2p_block_1_rate_kw=5.0,
            solver_p2p_block_1_start_hour=18,
            solver_p2p_block_1_end_hour=22,
            solver_p2p_bonus_price=0.3,
            solver_p2p_bonus_volume_kwh=10.0,
        )
        with patch.object(
            solver_writer,
            "fetch_entity_history_range",
            side_effect=self._fake_fetch,
        ):
            result = solver_writer.compute_nimbus_only_soc_counterfactual(cfg, DAY)

        self.assertIsNotNone(result)
        self.assertTrue(result["p2p_configured"])
        self.assertEqual(result["checkpoint_hour"], 18)
        self.assertIsNotNone(result["nimbus_only_soc_checkpoint_pct"])
        self.assertIsNotNone(result["real_soc_checkpoint_pct"])
        self.assertIsNotNone(result["viable_threshold_pct"])
        # 10kWh / 50kWh capacity * 1.1 margin = 22.0%
        self.assertAlmostEqual(result["viable_threshold_pct"], 22.0, places=1)


class TestPublish(unittest.TestCase):
    def test_publish_is_a_noop_when_compute_returns_none(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        with (
            patch.object(
                solver_writer, "ha_get", side_effect=urllib.error.URLError("not found")
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_nimbus_only_soc_counterfactual(
                cfg, datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
            )
        post.assert_not_called()

    def test_publish_skips_recompute_but_still_repushes_when_already_scored_for_yesterday(
        self,
    ):
        """Real fix (2026-08-30, issues #289/#292): the fast path must
        still re-push the SAME already-read state/attributes -- see
        test_daily_quality_report.py's own sibling test for the full
        "why" (skipping the publish entirely let this entity's
        freshness stamp go stale and get marked unavailable, forever)."""
        cfg = _cfg()
        now = datetime(2026, 8, 25, 10, 0, tzinfo=BRISBANE)
        existing = {"state": "42.0", "attributes": {"latest_date": "2026-08-24"}}
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(
                solver_writer, "compute_nimbus_only_soc_counterfactual"
            ) as compute,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_nimbus_only_soc_counterfactual(cfg, now)
        compute.assert_not_called()
        post.assert_called_once_with(
            solver_writer.COUNTERFACTUAL_ENTITY_ID,
            existing["state"],
            existing["attributes"],
        )
