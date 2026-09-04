"""Regression test for nimbus issue #356 (Mark Purcell, codebase review):
network.py's own _infeasible_plan() used to build every zero-filled array
field from the SAME single np.zeros(n) object -- verified live:
`plan.battery_charge_kw is plan.grid_import_kw` was True. Plan is
frozen=True (the dataclass itself is immutable), but that says nothing
about the mutability of the numpy arrays it holds -- any consumer doing
in-place arithmetic on one field of a non-optimal plan (a `+=`, or
`np.clip(..., out=...)`) would silently corrupt the other seven fields
too, since they're literally the same object in memory.

Real, direct call into the actual _infeasible_plan() -- not a
reimplementation -- same "solver/*.py has zero homeassistant.* imports,
directly importable" convention as this project's other solver test
files.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import PeriodGrid
from solver.network import _infeasible_plan


class TestInfeasiblePlanArrayAliasing(unittest.TestCase):
    def _make_plan(self):
        n = 5
        periods = PeriodGrid(
            hours=np.array([1.0] * n), start=datetime(2026, 1, 1, tzinfo=UTC)
        )
        return _infeasible_plan(periods, "infeasible", iterations=0)

    def test_no_two_array_fields_are_the_same_object(self):
        plan = self._make_plan()
        array_fields = [
            plan.battery_charge_kw,
            plan.battery_discharge_kw,
            plan.battery_soc_kwh,
            plan.grid_import_kw,
            plan.grid_export_kw,
            plan.export_bonus_kw,
            plan.solar_used_kw,
            plan.solar_curtailed_kw,
        ]
        for i, a in enumerate(array_fields):
            for j, b in enumerate(array_fields):
                if i != j:
                    self.assertIsNot(
                        a, b, f"array fields at index {i} and {j} alias the same object"
                    )

    def test_in_place_mutation_of_one_field_does_not_leak_into_another(self):
        # Plan is frozen=True, so `plan.grid_import_kw += 5.0` (a real
        # setattr, blocked by dataclasses.FrozenInstanceError) is not the
        # risk this issue describes -- the real risk is mutating the
        # ARRAY OBJECT directly (np.clip(..., out=...), a slice
        # assignment), which never touches __setattr__ at all and is
        # exactly what the frozen dataclass fails to protect against.
        plan = self._make_plan()
        plan.grid_import_kw[:] = 5.0
        self.assertTrue((plan.grid_import_kw == 5.0).all())
        # Every other field must still read as zero -- the real bug this
        # issue describes would have made ALL of them jump to 5.0 too.
        self.assertTrue((plan.battery_charge_kw == 0.0).all())
        self.assertTrue((plan.battery_discharge_kw == 0.0).all())
        self.assertTrue((plan.battery_soc_kwh == 0.0).all())
        self.assertTrue((plan.grid_export_kw == 0.0).all())
        self.assertTrue((plan.export_bonus_kw == 0.0).all())
        self.assertTrue((plan.solar_used_kw == 0.0).all())
        self.assertTrue((plan.solar_curtailed_kw == 0.0).all())


if __name__ == "__main__":
    unittest.main()
