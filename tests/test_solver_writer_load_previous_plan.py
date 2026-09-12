"""nimbus issue #785 (real production regression, found live within
minutes of v0.94.262/v0.94.263 deploying): #781's own fix added a new
required `Plan.soc_penalty_cost` field and updated every construction
site an earlier grep for the literal patterns "= Plan(" and
"return Plan(" found -- but `solver_writer.py`'s own
`load_previous_plan()` constructs its Plan as `network.Plan(...)`
(module-qualified), which neither of those two literal patterns ever
matched, so it was missed entirely. Every single solve cycle called
this function (via main()'s own `previous_plan = load_previous_plan()`)
and crashed with `TypeError: Plan.__init__() missing 1 required
positional argument: 'soc_penalty_cost'` -- a regression strictly worse
than the cosmetic cost-breakdown bug #781 itself fixed, since it took
the whole solver down.

Zero existing test exercised this function's own real file-found path
before this file: test_main_golden_output_guardrail.py's own
PLAN_STATE_PATH patch deliberately points at a nonexistent file, so
`load_previous_plan()` only ever hit its fast "file missing" return
None branch there, never the real json.load()/Plan(...) construction
this bug lived in.

This file closes that real coverage gap with a genuine save_plan_state()
-> load_previous_plan() round trip using the REAL functions, not a
hand-written JSON fixture -- exactly the path that broke live.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import solver_writer
from solver.elements import PeriodGrid
from solver.network import BatteryPlan, Plan


def _real_plan(n: int = 4) -> Plan:
    periods = PeriodGrid(hours=np.full(n, 1.0), start=datetime(2026, 9, 12, tzinfo=UTC))
    charge = np.array([0.0, 1.0, 0.0, 0.0])
    discharge = np.array([0.0, 0.0, 2.0, 0.0])
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=charge,
        battery_discharge_kw=discharge,
        battery_soc_kwh=np.full(n, 20.0),
        grid_import_kw=np.full(n, 1.0),
        grid_export_kw=np.zeros(n),
        export_bonus_kw=np.zeros(n),
        solar_used_kw=np.zeros(n),
        solar_curtailed_kw=np.zeros(n),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=12.5,
        soc_penalty_cost=0.0,
        iterations=3,
        batteries=[
            BatteryPlan(
                name="home",
                charge_kw=charge,
                discharge_kw=discharge,
                soc_kwh=np.full(n, 20.0),
            )
        ],
    )


class TestLoadPreviousPlanRealRoundTrip(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_save_then_load_does_not_crash_and_returns_a_real_plan(self):
        plan = _real_plan()
        with patch.object(solver_writer, "PLAN_STATE_PATH", self.path):
            solver_writer.save_plan_state(
                plan,
                period_hours_arr=[1.0, 1.0, 1.0, 1.0],
                period_start=plan.periods.start,
            )
            loaded = solver_writer.load_previous_plan()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "optimal")
        self.assertEqual(loaded.soc_penalty_cost, 0.0)
        np.testing.assert_array_equal(loaded.battery_charge_kw, plan.battery_charge_kw)
        np.testing.assert_array_equal(
            loaded.battery_discharge_kw, plan.battery_discharge_kw
        )
        self.assertEqual(len(loaded.batteries), 1)
        self.assertEqual(loaded.batteries[0].name, "home")

    def test_missing_file_returns_none_not_a_crash(self):
        with patch.object(
            solver_writer, "PLAN_STATE_PATH", "/tmp/nimbus_does_not_exist.json"
        ):
            self.assertIsNone(solver_writer.load_previous_plan())

    def test_non_optimal_saved_plan_returns_none(self):
        plan = _real_plan()
        plan = Plan(**{**plan.__dict__, "status": "infeasible"})
        with patch.object(solver_writer, "PLAN_STATE_PATH", self.path):
            solver_writer.save_plan_state(
                plan,
                period_hours_arr=[1.0, 1.0, 1.0, 1.0],
                period_start=plan.periods.start,
            )
            self.assertIsNone(solver_writer.load_previous_plan())
