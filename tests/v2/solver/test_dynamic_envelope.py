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


class TestGridConfigArrayOrScalarValidation(unittest.TestCase):
    def test_scalar_limits_are_unchanged(self):
        # Every existing caller -- byte-identical behaviour.
        grid = GridConfig(
            import_price=np.full(4, 0.30),
            export_price=np.full(4, 0.05),
            import_limit_kw=50.0,
            export_limit_kw=40.0,
        )
        self.assertEqual(grid.import_limit_kw, 50.0)
        self.assertEqual(grid.export_limit_kw, 40.0)

    def test_array_limits_are_accepted(self):
        grid = GridConfig(
            import_price=np.full(4, 0.30),
            export_price=np.full(4, 0.05),
            import_limit_kw=50.0,
            export_limit_kw=np.array([40.0, 40.0, 5.0, 5.0]),
        )
        np.testing.assert_array_equal(grid.export_limit_kw, [40.0, 40.0, 5.0, 5.0])

    def test_negative_array_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            GridConfig(
                import_price=np.full(4, 0.30),
                export_price=np.full(4, 0.05),
                import_limit_kw=50.0,
                export_limit_kw=np.array([40.0, -1.0, 5.0, 5.0]),
            )

    def test_wrong_length_array_is_rejected(self):
        with self.assertRaises(ValueError):
            GridConfig(
                import_price=np.full(4, 0.30),
                export_price=np.full(4, 0.05),
                import_limit_kw=50.0,
                export_limit_kw=np.array([40.0, 40.0, 5.0]),  # len 3, not 4
            )

    def test_fixed_export_kw_still_validates_against_a_per_period_envelope(self):
        # fixed_export_kw's own [0, export_limit_kw] check must compare
        # each period against THAT period's own real envelope value, not
        # one shared scalar -- a fixed commitment of 10kW in a period
        # whose own envelope is only 5kW must still be rejected.
        with self.assertRaises(ValueError):
            GridConfig(
                import_price=np.full(4, 0.30),
                export_price=np.full(4, 0.05),
                import_limit_kw=50.0,
                export_limit_kw=np.array([40.0, 40.0, 5.0, 5.0]),
                fixed_export_kw=np.array([np.nan, np.nan, 10.0, np.nan]),
            )

    def test_fixed_export_kw_within_the_tighter_envelope_period_is_accepted(self):
        grid = GridConfig(
            import_price=np.full(4, 0.30),
            export_price=np.full(4, 0.05),
            import_limit_kw=50.0,
            export_limit_kw=np.array([40.0, 40.0, 5.0, 5.0]),
            fixed_export_kw=np.array([np.nan, np.nan, 3.0, np.nan]),
        )
        self.assertEqual(grid.fixed_export_kw[2], 3.0)


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
    def test_a_uniform_array_envelope_matches_the_equivalent_scalar_plan(self):
        # A per-period array that happens to be constant across the
        # whole horizon must produce a BYTE-IDENTICAL plan to the plain
        # scalar it's equivalent to -- proves the new array path isn't
        # silently changing behaviour for the constant case.
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        solar = SolarConfig(forecast_kw=np.full(n, 10.0))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 3.0))]

        grid_scalar = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=50.0,
            export_limit_kw=20.0,
        )
        grid_array = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=np.full(n, 50.0),
            export_limit_kw=np.full(n, 20.0),
        )
        plan_scalar = build_plan(
            periods=periods,
            grid=grid_scalar,
            batteries=[_battery()],
            solar=solar,
            loads=loads,
        )
        plan_array = build_plan(
            periods=periods,
            grid=grid_array,
            batteries=[_battery()],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_scalar.status, plan_array.status)
        np.testing.assert_allclose(
            plan_scalar.grid_export_kw, plan_array.grid_export_kw
        )
        np.testing.assert_allclose(
            plan_scalar.grid_import_kw, plan_array.grid_import_kw
        )
        self.assertAlmostEqual(plan_scalar.total_cost, plan_array.total_cost, places=6)

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
