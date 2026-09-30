"""nimbus issue #1480: `Plan` reports the two LP-only tie-break costs its own
objective carried, so the scorer can separate them from a real LP-vs-evaluator
disagreement.

Measured on the reference household before this existed: of 28 Sep's $1.8388
`j_star_path_delta`, $1.61 was the #731 round-trip-loss tie-breaker and ~$0.21
the #692 battery charge-earliness tie-break -- unexplained residual -$0.0004.

Each field is checked against an INDEPENDENT recomputation from the plan's own
solved flows and the documented formulas, so the reported number cannot be
right by construction only.
"""

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import MIN_CHARGE_DISCHARGE_COST_SPREAD, build_plan


def _plan(earliness_budget=0.0, n=24):
    hours = np.full(n, 1.0)
    # Cheap early, expensive late: the LP charges then discharges, so both
    # tie-breaks are exercised. Prices chosen so the loss term sits BELOW
    # its cap on the charge side and AT its cap on the discharge side.
    imp = np.where(np.arange(n) < 12, 0.05, 0.40)
    grid = GridConfig(
        import_price=imp,
        export_price=np.full(n, 0.02),
        import_limit_kw=50.0,
        export_limit_kw=50.0,
    )
    battery = BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=0.0,
        max_soc_kwh=40.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.92,
        discharge_efficiency=0.92,
        charge_cost=0.005,
        discharge_cost=0.005,
        salvage_value=0.0,
    )
    plan = build_plan(
        periods=PeriodGrid(hours=hours, start=datetime(2026, 9, 28, tzinfo=UTC)),
        grid=grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 3.0))],
        battery_charge_earliness_budget_kw=earliness_budget,
    )
    return plan, hours, imp, battery


class TestTheLossTiebreakIsReported(unittest.TestCase):
    def test_it_equals_an_independent_recomputation(self):
        plan, hours, imp, b = _plan()
        self.assertTrue(plan.is_optimal)
        ch = np.asarray(plan.battery_charge_kw, dtype=float)
        dc = np.asarray(plan.battery_discharge_kw, dtype=float)
        self.assertGreater(float(ch.sum()), 1.0, "fixture must actually charge")
        self.assertGreater(float(dc.sum()), 1.0, "fixture must actually discharge")
        rate_c = np.minimum(
            imp * (1.0 - b.charge_efficiency), MIN_CHARGE_DISCHARGE_COST_SPREAD
        )
        rate_d = np.minimum(
            imp * (1.0 / b.discharge_efficiency - 1.0), MIN_CHARGE_DISCHARGE_COST_SPREAD
        )
        expected = float(np.sum(ch * rate_c * hours) + np.sum(dc * rate_d * hours))
        self.assertGreater(expected, 0.0)
        self.assertAlmostEqual(plan.battery_loss_tiebreak_cost, expected, places=6)

    def test_earliness_is_zero_when_its_budget_is_zero(self):
        plan, *_ = _plan(earliness_budget=0.0)
        self.assertEqual(plan.battery_charge_earliness_cost, 0.0)

    def test_earliness_equals_an_independent_recomputation(self):
        budget = 0.01
        plan, hours, _, _ = _plan(earliness_budget=budget)
        ch = np.asarray(plan.battery_charge_kw, dtype=float)
        elapsed = np.concatenate(([0.0], np.cumsum(hours)[:-1]))
        expected = float(np.sum(ch * (budget / hours.sum()) * elapsed * hours))
        # Primary channel: build_plan() routes to the secondary channel only
        # when solve_options is passed, and neither this fixture nor the
        # scorer's oracle passes it.
        self.assertAlmostEqual(plan.battery_charge_earliness_cost, expected, places=6)
        self.assertGreater(expected, 0.0)


class TestTheScorerCountsEveryLpOnlyTerm(unittest.TestCase):
    """Wiring: each Plan field must reach `j_star_path_delta_explained`."""

    def _explained_with(self, **bump):
        import dataclasses
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
        import test_jstar_path_delta as jt

        qr = sys.modules[jt.compute_quality_report.__module__]
        real = qr.build_plan

        def bp(*a, **k):
            plan = real(*a, **k)
            return dataclasses.replace(
                plan, **{f: getattr(plan, f) + v for f, v in bump.items()}
            )

        qr.build_plan = bp
        try:
            return jt._report().j_star_path_delta_explained
        finally:
            qr.build_plan = real

    def test_each_field_moves_explained_by_exactly_its_amount(self):
        base = self._explained_with()
        for field_name in (
            "battery_loss_tiebreak_cost",
            "battery_charge_earliness_cost",
            "soc_penalty_cost",
            "grid_import_excess_penalty_cost",
        ):
            with self.subTest(field_name):
                self.assertAlmostEqual(
                    self._explained_with(**{field_name: 0.5}) - base, 0.5, places=3
                )


if __name__ == "__main__":
    unittest.main()
