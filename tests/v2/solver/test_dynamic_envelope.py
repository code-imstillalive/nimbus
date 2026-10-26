"""The grid import and export limits may be a per-period dynamic operating
envelope as well as a scalar (#489, #493) (#1837).
"""

from __future__ import annotations

import unittest

import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _battery() -> BatteryConfig:
    return BatteryConfig(
        name="b",
        capacity_kwh=10.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=10.0,
        max_soc_kwh=10.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


class TestDynamicExportEnvelopeInTheRealLP(unittest.TestCase):
    def test_a_tighter_import_envelope_forces_the_same_excess_slack_390_already_has(
        self,
    ):
        # A per-period import envelope that dips below what the load
        # genuinely needs must still resolve via #390's own existing
        # grid_import_excess slack (optimal, not infeasible) -- proving
        # the array path reuses that same real safety valve, not a new
        # one.
        n = 3
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        import_limit = np.array([50.0, 2.0, 50.0])  # period 1 far below load
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=import_limit,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        unavailable_battery = BatteryConfig(
            name="offline",
            capacity_kwh=10.0,
            initial_soc_kwh=5.0,
            min_soc_kwh=0.0,
            max_soc_kwh=10.0,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
            available=False,
        )
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 10.0))]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[unavailable_battery],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertGreater(plan.import_cap_breach_kwh, 0.0)
        # Period 1 alone carries the whole breach (its own 2kW cap vs
        # 10kW load = 8kW of forced excess); periods 0/2 have no breach.
        self.assertAlmostEqual(float(plan.grid_import_excess_kw[1]), 8.0, places=6)
        self.assertEqual(float(plan.grid_import_excess_kw[0]), 0.0)
        self.assertEqual(float(plan.grid_import_excess_kw[2]), 0.0)


if __name__ == "__main__":
    unittest.main()
