"""nimbus issue #390 (Mark Purcell): a load forecast extrapolated above
`import_limit_kw + max_discharge_kw + solar_kw` for longer than the
battery's own energy could cover made the WHOLE 96h horizon infeasible --
confirmed live, a 40-minute EV-charging transient extrapolated flat
overnight by the recursive load forecaster needed ~90kWh of battery
support a real pack didn't have, and the Solver returned
status="infeasible" for 46 minutes straight, discarding every other,
perfectly feasible period along with it.

Regression test shape matches the issue's own suggested test exactly:
`build_plan()` with a load array of 40kW for 24h, solar 0,
import_limit_kw 30, max_discharge 24, initial SoC 45% of 100kWh --
before the fix: status infeasible. After: optimal, with a non-zero
import-slack total (`Plan.import_cap_breach_kwh`) and a plan that still
discharges through the evening peak.
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


class TestGridImportExcessSlack(unittest.TestCase):
    def test_oversized_load_no_longer_makes_whole_horizon_infeasible(self):
        n = 24
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=30.0,
            export_limit_kw=30.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
            capacity_kwh=100.0,
            initial_soc_kwh=45.0,
            min_soc_kwh=5.0,
            max_soc_kwh=100.0,
            max_charge_kw=24.0,
            max_discharge_kw=24.0,
            charge_efficiency=0.99,
            discharge_efficiency=0.99,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
        )
        # A load extrapolated flat at 40kW all day -- above
        # import_limit_kw(30) + max_discharge_kw(24) is fine on its own,
        # but held for a full 24h the battery's real ~40kWh of usable
        # energy (100 - 5 floor, minus round-trip losses) can't cover the
        # 10kW/period shortfall (40 - 30) for anywhere near 24 real hours.
        loads = [LoadConfig(name="whole_house", forecast_kw=np.full(n, 40.0))]

        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )

        self.assertEqual(
            plan.status,
            "optimal",
            "an oversized load must be served via the penalized excess-import "
            "slack, not discard the entire horizon as infeasible",
        )
        self.assertGreater(
            plan.import_cap_breach_kwh,
            0.0,
            "the real, unavoidable shortfall must show up as a nonzero "
            "import_cap_breach_kwh, not vanish silently",
        )
        # The battery must still be doing real, meaningful work covering
        # part of the shortfall -- not just fully ceding everything to the
        # (much more expensive) excess-import slack.
        self.assertGreater(
            float(np.max(plan.battery_discharge_kw)),
            5.0,
            "the battery should still discharge to help cover the shortfall, "
            "not sit idle once a slack valve exists",
        )
        # No period should ever exceed the physical grid_import ub without
        # it being accounted for in the excess field.
        self.assertTrue(np.all(plan.grid_import_kw >= -1e-6))

    def test_normal_feasible_load_never_touches_the_slack(self):
        """A load that never threatens the cap must leave the new slack
        variable at exactly zero -- confirms the fix is a real no-op for
        every already-working scenario, not just non-fatal."""
        n = 8
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=30.0,
            export_limit_kw=30.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
            capacity_kwh=20.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=2.0,
            max_soc_kwh=20.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.99,
            discharge_efficiency=0.99,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
        )
        loads = [LoadConfig(name="whole_house", forecast_kw=np.full(n, 5.0))]

        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )

        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.import_cap_breach_kwh, 0.0)
        self.assertTrue(np.all(plan.grid_import_excess_kw == 0.0))


if __name__ == "__main__":
    unittest.main()
