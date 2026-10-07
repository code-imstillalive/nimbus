"""nimbus #238 / #1535: one grid connection and one battery each carry current
one way per period.

The LP's linear caps (#245 battery, #266 grid) bound opposing flows but do not
forbid them, so a profitable spread (export price above import price) planned
import with export, and charge with discharge, in the same period: #1535's
reduced case showed 24 of 24 grid and 23 of 24 battery periods. `build_plan()`
now re-solves once with a direction binary per period per participant when the
first plan has opposing flows, and returns plans that are already one-way
unchanged.

The scenarios follow #1535's `reproduce_opposing_flows.py` (Mark Purcell):
120 kWh battery, 36 kW both ways, 94% round trip, 32/30 kW grid, 0.357 kW load.
"""

import math
import unittest
from datetime import datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import _build_plan_thermal_fallback, build_plan

TOL = 1e-6
START = datetime.fromisoformat("2026-10-05T17:00:00+10:00")


def _battery(name="home", soc=118.8, max_charge=36.0, max_discharge=36.0):
    return BatteryConfig(
        name=name,
        capacity_kwh=120.0,
        initial_soc_kwh=soc,
        min_soc_kwh=6.0,
        max_soc_kwh=118.8,
        max_charge_kw=max_charge,
        max_discharge_kw=max_discharge,
        charge_efficiency=math.sqrt(0.94),
        discharge_efficiency=math.sqrt(0.94),
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.15,
    )


def _kwargs(hours, imports, exports, fixed=None, batteries=None, load=0.357):
    n = len(hours)
    return {
        "periods": PeriodGrid(hours=np.array(hours), start=START),
        "grid": GridConfig(
            import_price=np.array(imports, dtype=float),
            export_price=np.array(exports, dtype=float),
            import_limit_kw=32.0,
            export_limit_kw=30.0,
            fixed_export_kw=None if fixed is None else np.array(fixed, dtype=float),
        ),
        "batteries": batteries or [_battery()],
        "solar": SolarConfig(forecast_kw=np.zeros(n)),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(n, load))],
    }


def _grid_opposing(plan):
    ordinary = plan.grid_import_kw - (
        plan.grid_import_excess_kw if plan.grid_import_excess_kw.size else 0.0
    )
    return int(np.sum(np.minimum(ordinary, plan.grid_export_kw) > TOL))


def _battery_opposing(plan):
    return {
        b.name: int(np.sum(np.minimum(b.charge_kw, b.discharge_kw) > TOL))
        for b in plan.batteries
    }


class TestTheProfitableSpread(unittest.TestCase):
    """#1535's counterexample: constant 21.16c import, 61.66c export."""

    def test_the_old_formulation_really_had_opposing_flows(self):
        first = _build_plan_thermal_fallback(
            **_kwargs([0.5] * 24, [0.2116] * 24, [0.6166] * 24)
        )
        self.assertEqual(first.status, "optimal")
        self.assertGreater(_grid_opposing(first), 0)
        self.assertGreater(_battery_opposing(first)["home"], 0)

    def test_the_published_plan_is_one_way(self):
        plan = build_plan(**_kwargs([0.5] * 24, [0.2116] * 24, [0.6166] * 24))
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(_grid_opposing(plan), 0)
        self.assertEqual(_battery_opposing(plan), {"home": 0})
        self.assertTrue(plan.direction_exclusivity_enforced)

    def test_genuine_high_export_is_still_taken(self):
        """Exclusivity must not be bought by suppressing the real price:
        the battery still discharges to export at 61.66c."""
        plan = build_plan(**_kwargs([0.5] * 24, [0.2116] * 24, [0.6166] * 24))
        self.assertGreater(float(np.sum(plan.grid_export_kw * 0.5)), 50.0)


class TestFixedBlocksStayNet(unittest.TestCase):
    """#1535's fixed 14/4/17 kW evening blocks (#1610 made them net)."""

    def test_blocks_are_delivered_net(self):
        hours = [3.0, 1.0, 3.0]
        plan = build_plan(
            **_kwargs(hours, [0.0065] * 3, [0.5321] * 3, fixed=[14.0, 4.0, 17.0])
        )
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_allclose(plan.grid_export_kw, [14.0, 4.0, 17.0], atol=TOL)
        net = float(
            np.sum((plan.grid_export_kw - plan.grid_import_kw) * np.array(hours))
        )
        self.assertAlmostEqual(net, 97.0, places=6)
        self.assertEqual(_grid_opposing(plan), 0)


class TestCleanPlansAreUntouched(unittest.TestCase):
    def test_an_ordinary_price_plan_is_identical_and_not_flagged(self):
        kw = _kwargs([0.5] * 6, [0.30] * 6, [0.05] * 6)
        plan = build_plan(**kw)
        first = _build_plan_thermal_fallback(**kw)
        self.assertFalse(plan.direction_exclusivity_enforced)
        for field in (
            "grid_import_kw",
            "grid_export_kw",
            "battery_charge_kw",
            "battery_discharge_kw",
        ):
            np.testing.assert_array_equal(getattr(plan, field), getattr(first, field))

    def test_zero_and_negative_prices_stay_one_way(self):
        imports = [0.0, -0.05, 0.10, 0.25, -0.02, 0.30]
        exports = [-0.01, -0.08, 0.04, 0.20, -0.04, 0.45]
        plan = build_plan(**_kwargs([0.5] * 6, imports, exports))
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(_grid_opposing(plan), 0)
        self.assertEqual(_battery_opposing(plan), {"home": 0})


class TestPerParticipant(unittest.TestCase):
    def test_each_battery_is_one_way_and_unequal_caps_are_respected(self):
        batteries = [
            _battery("home", max_charge=36.0, max_discharge=20.0),
            _battery("ev", soc=40.0, max_charge=11.0, max_discharge=7.0),
        ]
        plan = build_plan(
            **_kwargs([0.5] * 24, [0.2116] * 24, [0.6166] * 24, batteries=batteries)
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(_battery_opposing(plan), {"home": 0, "ev": 0})
        self.assertEqual(_grid_opposing(plan), 0)
        by_name = {b.name: b for b in plan.batteries}
        self.assertLessEqual(float(by_name["home"].discharge_kw.max()), 20.0 + TOL)
        self.assertLessEqual(float(by_name["ev"].charge_kw.max()), 11.0 + TOL)
        self.assertLessEqual(float(by_name["ev"].discharge_kw.max()), 7.0 + TOL)

    def test_binaries_are_never_pooled_across_batteries(self):
        """Exclusivity is per battery: the constraint names carry the
        battery, so one charging while another discharges stays legal."""
        import inspect

        from solver import network

        src = inspect.getsource(network._build_plan_once)
        self.assertIn('f"battery_dir_{b.name}_{t}"', src)


if __name__ == "__main__":
    unittest.main()
