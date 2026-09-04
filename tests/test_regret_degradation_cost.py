"""Regression test for a real finding from Mark Purcell's live dispatch-
report tooling (nimbus issue #336, 2026-09-04 dashboard analysis, later
confirmed against his own analysis-checklist.md): `evaluate_realized_
cost()` (regret.py) priced `charge_cost`/`discharge_cost` only, with no
way to express `degradation_cost_per_kwh` -- a real, separate BatteryConfig
field the LIVE LP (network.py's build_plan()) already adds to both
charge and discharge cost terms. For an install with a nonzero
degradation_cost_per_kwh configured, `sensor.nimbus_solver_quality_
report`'s own j_ref/j_ach/j_star all scored a battery that cycles for
free, while the real dispatch optimizes against a genuinely more
expensive one -- inflating the reported regret_dollars figure (the
oracle in particular over-cycles for "free" arbitrage that would never
be worthwhile once degradation is priced in).

Fixed by adding a `degradation_cost_per_kwh` parameter to evaluate_
realized_cost() itself, applied identically to network.py's own
formula (added to BOTH charge and discharge cost arrays). Zero by
default -- a complete no-op for any install that doesn't configure it.

Real, direct calls into the actual evaluate_realized_cost() -- not a
reimplementation -- same "solver/*.py has zero homeassistant.*
imports, directly importable" convention as this project's other
solver test files.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.regret import evaluate_realized_cost


class TestEvaluateRealizedCostDegradationCost(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        n = 4
        kwargs = {
            "hours": np.full(n, 1.0),
            "load_real_kw": np.full(n, 2.0),
            "solar_real_kw": np.zeros(n),
            "import_price_real": np.full(n, 0.30),
            "export_price_real": np.full(n, 0.10),
            "charge_committed_kw": np.zeros(n),
            "discharge_committed_kw": np.zeros(n),
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "final_soc_kwh": 50.0,
            "salvage_value": 0.0,
            "grid_import_limit_kw": 40.0,
            "grid_export_limit_kw": 40.0,
        }
        kwargs.update(overrides)
        return kwargs

    def test_zero_throughput_is_unaffected_by_degradation_cost(self):
        """j_ref-shaped case: idle battery, zero real cycling -- a
        nonzero degradation_cost_per_kwh must contribute nothing, since
        there's no throughput for it to apply to."""
        zero_deg = evaluate_realized_cost(**self._base_kwargs())
        with_deg = evaluate_realized_cost(
            **self._base_kwargs(degradation_cost_per_kwh=0.03)
        )
        self.assertAlmostEqual(zero_deg.total_cost, with_deg.total_cost, places=9)

    def test_default_is_zero_and_a_complete_noop(self):
        """Omitting the new parameter entirely must be byte-identical to
        passing 0.0 explicitly -- backward compatible for every existing
        caller (forecast_regret.py included) that doesn't pass it."""
        n = 4
        omitted = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.full(n, 3.0),
                discharge_committed_kw=np.zeros(n),
            )
        )
        explicit_zero = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.full(n, 3.0),
                discharge_committed_kw=np.zeros(n),
                degradation_cost_per_kwh=0.0,
            )
        )
        self.assertEqual(omitted.total_cost, explicit_zero.total_cost)

    def test_real_throughput_pays_the_configured_degradation_cost(self):
        """A real dispatch trajectory (some charge, some discharge) must
        pay degradation_cost_per_kwh on its own TOTAL real throughput
        (charge + discharge), matching network.py's own live LP formula
        exactly -- added to BOTH charge_cost and discharge_cost, not
        just one direction."""
        n = 4
        charge_kw = np.array([5.0, 0.0, 0.0, 0.0])
        discharge_kw = np.array([0.0, 0.0, 3.0, 0.0])
        hours = np.full(n, 1.0)
        deg = 0.03

        zero_deg = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=charge_kw,
                discharge_committed_kw=discharge_kw,
                hours=hours,
            )
        )
        with_deg = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=charge_kw,
                discharge_committed_kw=discharge_kw,
                hours=hours,
                degradation_cost_per_kwh=deg,
            )
        )
        # Real total throughput: 5kWh charge + 3kWh discharge = 8kWh.
        expected_extra_cost = deg * float(
            np.sum(charge_kw * hours) + np.sum(discharge_kw * hours)
        )
        self.assertAlmostEqual(
            with_deg.total_cost - zero_deg.total_cost,
            expected_extra_cost,
            places=9,
        )

    def test_more_throughput_pays_proportionally_more(self):
        """A trajectory that cycles the battery twice as much must pay
        exactly twice the degradation cost -- confirms the term scales
        with real throughput, not a flat penalty."""
        n = 4
        hours = np.full(n, 1.0)
        deg = 0.05

        light = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.array([2.0, 0.0, 0.0, 0.0]),
                discharge_committed_kw=np.zeros(n),
                hours=hours,
                degradation_cost_per_kwh=deg,
            )
        )
        heavy = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.array([4.0, 0.0, 0.0, 0.0]),
                discharge_committed_kw=np.zeros(n),
                hours=hours,
                degradation_cost_per_kwh=deg,
            )
        )
        light_zero_deg = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.array([2.0, 0.0, 0.0, 0.0]),
                discharge_committed_kw=np.zeros(n),
                hours=hours,
            )
        )
        heavy_zero_deg = evaluate_realized_cost(
            **self._base_kwargs(
                charge_committed_kw=np.array([4.0, 0.0, 0.0, 0.0]),
                discharge_committed_kw=np.zeros(n),
                hours=hours,
            )
        )
        light_deg_cost = light.total_cost - light_zero_deg.total_cost
        heavy_deg_cost = heavy.total_cost - heavy_zero_deg.total_cost
        self.assertAlmostEqual(heavy_deg_cost, 2 * light_deg_cost, places=9)
