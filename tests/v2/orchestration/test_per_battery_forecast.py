"""Each battery's own charge, discharge and SoC series, published beside the
plan (#563) (#1837).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import solver_publish


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
    def test_a_participant_missing_from_capacity_map_gets_zero_not_a_crash(self):
        plan = SimpleNamespace(
            batteries=[_battery_plan("pool_ev", [0.0], [0.0], [5.0])]
        )
        result = solver_publish.build_per_battery_forecast(
            plan,
            _grid_times(1),
            1,
            {},  # no capacity known for "pool_ev"
        )
        self.assertEqual(result[0]["forecast"][0]["soc_pct"], 0.0)
        self.assertEqual(result[0]["forecast"][0]["soc_kwh"], 5.0)


if __name__ == "__main__":
    unittest.main()
