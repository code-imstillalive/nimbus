"""Direct test coverage for solver_writer.py's publish_flex_signals()
(nimbus issue #496, Signals 7/7 of #489) -- the grid-operator/load-ranging
flex signals (#491/#492's own already-shipped GridSignals/BatterySignals/
LoadSignals), published as real entities for the first time.

Uses lightweight stand-ins (SimpleNamespace) for Plan/GridSignals/
BatterySignals/LoadSignals rather than a real solved Plan -- the
publish/serialization logic under test here is independent of how the
ranging numbers were actually computed; solver/network.py's own tests
already cover that.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _grid_signals(
    *,
    grid_import_headroom_kw=(2.5,),
    grid_import_headroom_kwh=(0.625,),
    grid_export_headroom_kw=(0.0,),
    grid_export_headroom_kwh=(0.0,),
    forced_import_cost=(0.12,),
    forced_export_cost=(-0.05,),
    flex_available_up_kw=(2.5,),
    flex_available_down_kw=(0.0,),
    load_headroom_up_kwh=(1.1,),
    load_headroom_down_kwh=(0.3,),
):
    return SimpleNamespace(
        grid_import_headroom_kw=grid_import_headroom_kw,
        grid_import_headroom_kwh=grid_import_headroom_kwh,
        grid_export_headroom_kw=grid_export_headroom_kw,
        grid_export_headroom_kwh=grid_export_headroom_kwh,
        forced_import_cost=forced_import_cost,
        forced_export_cost=forced_export_cost,
        flex_available_up_kw=flex_available_up_kw,
        flex_available_down_kw=flex_available_down_kw,
        load_headroom_up_kwh=load_headroom_up_kwh,
        load_headroom_down_kwh=load_headroom_down_kwh,
    )


def _battery_signal(
    name="home",
    available_up_kw=(3.0,),
    available_down_kw=(4.0,),
    available_up_ranging_kw=(2.5,),
    available_down_ranging_kw=(4.0,),
):
    return SimpleNamespace(
        name=name,
        available_up_kw=available_up_kw,
        available_down_kw=available_down_kw,
        available_up_ranging_kw=available_up_ranging_kw,
        available_down_ranging_kw=available_down_ranging_kw,
    )


def _load_signal(
    name="pool_pump",
    intent=("UNLIMIT",),
    band_min_kw=(0.0,),
    band_max_kw=(1.5,),
    reduced_cost_per_kwh=(0.0,),
    degenerate=(False,),
):
    return SimpleNamespace(
        name=name,
        intent=intent,
        band_min_kw=band_min_kw,
        band_max_kw=band_max_kw,
        reduced_cost_per_kwh=reduced_cost_per_kwh,
        degenerate=degenerate,
    )


def _plan(*, grid_signals=None, battery_signals=None, load_signals=None):
    return SimpleNamespace(
        grid_signals=grid_signals,
        battery_signals=battery_signals if battery_signals is not None else [],
        load_signals=load_signals if load_signals is not None else [],
    )


class TestPublishFlexSignals(unittest.TestCase):
    def test_no_op_when_grid_signals_absent(self):
        # The real, common case: switch.nimbus_solver_flex_signals_enabled
        # off, so build_plan() never populated grid_signals at all.
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(_plan(grid_signals=None))
        post.assert_not_called()

    def test_publishes_grid_level_scalars(self):
        plan = _plan(grid_signals=_grid_signals())
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan)
        post.assert_called_once()
        entity_id, state, attrs = post.call_args[0]
        self.assertEqual(entity_id, "sensor.nimbus_flex_signals")
        # State mirrors flex_available_up_kw[0], the household-level figure.
        self.assertEqual(state, 2.5)
        self.assertEqual(attrs["grid_import_headroom_kw"], 2.5)
        self.assertEqual(attrs["grid_import_headroom_kwh"], 0.625)
        self.assertEqual(attrs["grid_export_headroom_kw"], 0.0)
        self.assertEqual(attrs["forced_import_cost"], 0.12)
        self.assertEqual(attrs["forced_export_cost"], -0.05)
        self.assertEqual(attrs["flex_available_up_kw"], 2.5)
        self.assertEqual(attrs["flex_available_down_kw"], 0.0)
        self.assertEqual(attrs["load_headroom_up_kwh"], 1.1)
        self.assertEqual(attrs["load_headroom_down_kwh"], 0.3)
        self.assertEqual(attrs["unit_of_measurement"], "kW")
        self.assertIn("generated_at", attrs)

    def test_publishes_per_battery_signals(self):
        plan = _plan(
            grid_signals=_grid_signals(),
            battery_signals=[_battery_signal(name="home"), _battery_signal(name="ev1")],
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan)
        _, _, attrs = post.call_args[0]
        self.assertEqual(len(attrs["battery_signals"]), 2)
        self.assertEqual(attrs["battery_signals"][0]["name"], "home")
        self.assertEqual(attrs["battery_signals"][0]["available_up_kw"], 3.0)
        self.assertEqual(attrs["battery_signals"][0]["available_down_kw"], 4.0)
        self.assertEqual(attrs["battery_signals"][0]["available_up_ranging_kw"], 2.5)
        self.assertEqual(attrs["battery_signals"][1]["name"], "ev1")

    def test_battery_signal_ranging_none_when_ranging_invalid(self):
        # BatterySignals' own PHYSICAL fields are always populated; the
        # ranging-derived pair can independently be None (see that
        # dataclass's own docstring) -- confirm this doesn't crash and
        # publishes an honest None, not a fabricated 0.0.
        plan = _plan(
            grid_signals=_grid_signals(),
            battery_signals=[
                _battery_signal(
                    available_up_ranging_kw=None, available_down_ranging_kw=None
                )
            ],
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan)
        _, _, attrs = post.call_args[0]
        self.assertIsNone(attrs["battery_signals"][0]["available_up_ranging_kw"])
        self.assertIsNone(attrs["battery_signals"][0]["available_down_ranging_kw"])

    def test_publishes_per_load_signals(self):
        plan = _plan(
            grid_signals=_grid_signals(),
            load_signals=[
                _load_signal(name="pool_pump", intent=("UNLIMIT",)),
                _load_signal(
                    name="hws",
                    intent=("SET",),
                    band_min_kw=(2.0,),
                    band_max_kw=(2.0,),
                    reduced_cost_per_kwh=(0.08,),
                    degenerate=(True,),
                ),
            ],
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan)
        _, _, attrs = post.call_args[0]
        self.assertEqual(len(attrs["load_signals"]), 2)
        self.assertEqual(attrs["load_signals"][0]["name"], "pool_pump")
        self.assertEqual(attrs["load_signals"][0]["intent"], "UNLIMIT")
        second = attrs["load_signals"][1]
        self.assertEqual(second["name"], "hws")
        self.assertEqual(second["intent"], "SET")
        self.assertEqual(second["band_min_kw"], 2.0)
        self.assertEqual(second["band_max_kw"], 2.0)
        self.assertEqual(second["reduced_cost_per_kwh"], 0.08)
        self.assertTrue(second["degenerate"])

    def test_no_battery_or_load_signals_gives_empty_lists_not_a_crash(self):
        plan = _plan(grid_signals=_grid_signals())
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan)
        _, _, attrs = post.call_args[0]
        self.assertEqual(attrs["battery_signals"], [])
        self.assertEqual(attrs["load_signals"], [])


if __name__ == "__main__":
    unittest.main()
