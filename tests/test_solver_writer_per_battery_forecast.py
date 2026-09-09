"""nimbus issue #563 item 5: direct test coverage for solver_writer.
build_per_battery_forecast() -- Plan.batteries[]'s own real per-
participant charge/discharge/SoC series, published as a new "batteries"
attribute on sensor.nimbus_solver_battery_forecast (exposure only, the
solver-level data already existed via network.py's own #467 stage 1
BatteryPlan). Bare SimpleNamespace plan fakes, same convention this
file's own siblings already use for isolated helper tests.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

import _solver_path  # noqa: F401
import numpy as np
import solver_writer


def _battery_plan(name: str, charge_kw, discharge_kw, soc_kwh):
    return SimpleNamespace(
        name=name,
        charge_kw=np.asarray(charge_kw, dtype=np.float64),
        discharge_kw=np.asarray(discharge_kw, dtype=np.float64),
        soc_kwh=np.asarray(soc_kwh, dtype=np.float64),
    )


def _grid_times(n: int) -> list[datetime]:
    start = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    return [start] * n  # times aren't under test here, just need n of them


class TestBuildPerBatteryForecast(unittest.TestCase):
    def test_single_home_battery_matches_the_aggregate_shape(self):
        plan = SimpleNamespace(
            batteries=[_battery_plan("home", [5.0, 0.0], [0.0, 1.3], [20.0, 18.7])]
        )
        result = solver_writer.build_per_battery_forecast(
            plan, _grid_times(2), 2, {"home": 40.0}
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "home")
        self.assertEqual(len(result[0]["forecast"]), 2)
        row0 = result[0]["forecast"][0]
        self.assertEqual(row0["charge_kw"], 5.0)
        self.assertEqual(row0["discharge_kw"], 0.0)
        self.assertEqual(row0["soc_kwh"], 20.0)
        self.assertEqual(row0["soc_pct"], 50.0)

    def test_each_participant_uses_its_own_real_capacity_not_the_fleet_total(self):
        # nimbus issue #569's own real bug, restated for THIS field: a
        # 40.3 kWh home pack + a 60 kWh EV must never divide either
        # participant's own soc_pct by the 100.3 kWh fleet total.
        plan = SimpleNamespace(
            batteries=[
                _battery_plan("home", [0.0], [0.0], [20.15]),  # 50% of 40.3
                _battery_plan("ev_m3p", [0.0], [0.0], [30.0]),  # 50% of 60.0
            ]
        )
        result = solver_writer.build_per_battery_forecast(
            plan, _grid_times(1), 1, {"home": 40.3, "ev_m3p": 60.0}
        )
        by_name = {b["name"]: b for b in result}
        self.assertAlmostEqual(
            by_name["home"]["forecast"][0]["soc_pct"], 50.0, places=2
        )
        self.assertAlmostEqual(
            by_name["ev_m3p"]["forecast"][0]["soc_pct"], 50.0, places=2
        )

    def test_a_participant_missing_from_capacity_map_gets_zero_not_a_crash(self):
        plan = SimpleNamespace(
            batteries=[_battery_plan("pool_ev", [0.0], [0.0], [5.0])]
        )
        result = solver_writer.build_per_battery_forecast(
            plan,
            _grid_times(1),
            1,
            {},  # no capacity known for "pool_ev"
        )
        self.assertEqual(result[0]["forecast"][0]["soc_pct"], 0.0)
        self.assertEqual(result[0]["forecast"][0]["soc_kwh"], 5.0)

    def test_a_zero_capacity_participant_gets_zero_not_a_zero_division_crash(self):
        plan = SimpleNamespace(
            batteries=[_battery_plan("misconfigured", [0.0], [0.0], [0.0])]
        )
        result = solver_writer.build_per_battery_forecast(
            plan, _grid_times(1), 1, {"misconfigured": 0.0}
        )
        self.assertEqual(result[0]["forecast"][0]["soc_pct"], 0.0)

    def test_no_batteries_returns_an_empty_list(self):
        plan = SimpleNamespace(batteries=[])
        result = solver_writer.build_per_battery_forecast(plan, _grid_times(3), 3, {})
        self.assertEqual(result, [])

    def test_time_field_matches_grid_times_isoformat(self):
        times = _grid_times(2)
        plan = SimpleNamespace(
            batteries=[_battery_plan("home", [0.0, 0.0], [0.0, 0.0], [10.0, 10.0])]
        )
        result = solver_writer.build_per_battery_forecast(
            plan, times, 2, {"home": 20.0}
        )
        self.assertEqual(result[0]["forecast"][0]["time"], times[0].isoformat())
        self.assertEqual(result[0]["forecast"][1]["time"], times[1].isoformat())


if __name__ == "__main__":
    unittest.main()
