"""The plan's direction check reports its outcome on the plan and on the
battery forecast sensor, not only in the log (#238, #1535, #1751) (#1837).
"""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
from solver import network
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import _build_plan_thermal_fallback, build_plan

# #1535's reproduction (Mark Purcell): 120 kWh battery, 36 kW both ways, 94%
# round trip, 32/30 kW grid, 0.357 kW load. Moved here from the #238 file when
# #1837's cutover retired it; the #238 checks themselves are in tests/v2.
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


SPREAD = ([0.5] * 24, [0.2116] * 24, [0.6166] * 24)  # #1535's counterexample


def _patched(strict_status=None, strict_result=None):
    """Run the real first solve; replace every direction-exclusive re-solve
    with a plan of `strict_status`, or with `strict_result(real_strict)`."""
    real = _build_plan_thermal_fallback

    def fake(**kw):
        if "direction_binaries" not in kw:
            return real(**kw)
        if strict_result is not None:
            return strict_result(real(**kw))
        return replace(real(**kw), status=strict_status)

    return mock.patch.object(network, "_build_plan_thermal_fallback", fake)


class TestOutcomeOnEveryPath(unittest.TestCase):
    def test_a_one_way_first_solve_is_not_needed(self):
        plan = build_plan(**_kwargs([0.5] * 6, [0.30] * 6, [0.05] * 6))
        self.assertEqual(plan.direction_exclusivity, "not_needed")
        self.assertEqual(plan.direction_two_way_pairs, 0)
        self.assertFalse(plan.direction_exclusivity_enforced)

    def test_a_re_solve_that_makes_it_one_way_is_enforced(self):
        plan = build_plan(**_kwargs(*SPREAD))
        self.assertEqual(plan.direction_exclusivity, "enforced")
        self.assertEqual(plan.direction_two_way_pairs, 0)
        self.assertTrue(plan.direction_exclusivity_enforced)

    def test_a_declined_re_solve_is_visible_and_counted(self):
        first = _build_plan_thermal_fallback(**_kwargs(*SPREAD))
        expected = len(network._opposing_flow_binaries(first))
        self.assertGreater(expected, 0)
        with (
            _patched(strict_status="time_limit"),
            mock.patch.object(network._LOGGER, "warning") as warn,
        ):
            plan = build_plan(**_kwargs(*SPREAD))
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.direction_exclusivity, "declined")
        self.assertEqual(plan.direction_two_way_pairs, expected)
        self.assertFalse(plan.direction_exclusivity_enforced)
        # the plan kept is the first solve's, still two-way
        self.assertGreater(_grid_opposing(plan), 0)
        np.testing.assert_array_equal(plan.grid_export_kw, first.grid_export_kw)
        warn.assert_called_once()
        self.assertIn("time_limit", warn.call_args.args)

    def test_pairs_left_after_every_re_solve_are_still_two_way(self):
        # every re-solve "optimal" but handing back the two-way first plan
        first = _build_plan_thermal_fallback(**_kwargs(*SPREAD))
        with (
            _patched(strict_result=lambda _strict: first),
            mock.patch.object(network._LOGGER, "warning") as warn,
        ):
            plan = build_plan(**_kwargs(*SPREAD))
        self.assertEqual(plan.direction_exclusivity, "still_two_way")
        self.assertEqual(
            plan.direction_two_way_pairs,
            len(network._opposing_flow_binaries(first)),
        )
        warn.assert_called_once()

    def test_a_non_optimal_first_solve_is_not_checked(self):
        def first_fails(**kw):
            return replace(_build_plan_thermal_fallback(**kw), status="infeasible")

        with mock.patch.object(network, "_build_plan_thermal_fallback", first_fails):
            plan = build_plan(**_kwargs(*SPREAD))
        self.assertEqual(plan.direction_exclusivity, "not_checked")
        self.assertEqual(plan.direction_two_way_pairs, 0)


class TestPublished(unittest.TestCase):
    def test_battery_forecast_carries_both_fields(self):
        src = (Path(network.__file__).parent.parent / "solver_publish.py").read_text(
            encoding="utf-8"
        )
        block = src[src.index('"batteries": _diag_757_battery_forecast,') :][:600]
        self.assertIn('"direction_exclusivity": getattr(', block)
        self.assertIn('"direction_two_way_pairs": getattr(', block)


if __name__ == "__main__":
    unittest.main()
