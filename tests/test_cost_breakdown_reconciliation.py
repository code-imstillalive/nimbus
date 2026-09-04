"""Real regression test, found live on devhub (2026-08-25) while
independently verifying nimbus issue #149 -- Mark Purcell's own executable
reconciliation tests (run against v0.80 and re-confirmed against v0.81.0)
proved `total_cost` could not be reconstructed from anything else in the
solver diagnostics dump. `compute_cost_breakdown()` (solver_writer.py)
fixes this by exposing named grid_net/degradation/charge_fee/
discharge_fee/terminal_value_credit fields that reconcile exactly to
total_cost by construction.

This test covers the actual arithmetic, not just "it doesn't crash" --
in particular the discharge_fee case, which must sum an hour-varying
discharge_cost_arr per period (a real household's own LocalVolts
schedule) rather than multiplying a flat scalar by total kWh, and the
terminal_value_credit case, which must capture the REAL residual
(salvage/terminal-value credit) rather than assuming it's zero.
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_writer


class TestCostBreakdownReconciliation(unittest.TestCase):
    def test_reconciles_exactly_to_total_cost(self):
        # 4 periods, flat 0.25h each, no degradation, flat discharge cost.
        net_costs = [1.0, -0.5, 2.0, 0.3]
        total_cost = 5.0
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=net_costs,
            total_cost=total_cost,
            degradation_cost_per_kwh=0.0,
            total_throughput_kwh=100.0,
            charge_cost=0.005,
            total_charge_kwh=50.0,
            discharge_cost_arr=np.full(4, 0.01),
            battery_discharge_kw=np.array([2.0, 2.0, 2.0, 2.0]),
            period_hours=np.full(4, 0.25),
        )
        reconciled = (
            breakdown["grid_net"]
            + breakdown["degradation"]
            + breakdown["charge_fee"]
            + breakdown["discharge_fee"]
            + breakdown["terminal_value_credit"]
        )
        self.assertAlmostEqual(reconciled, total_cost, places=6)

    def test_grid_net_is_sum_of_supplied_net_costs(self):
        net_costs = [1.5, -2.25, 0.75]
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=net_costs,
            total_cost=0.0,
            degradation_cost_per_kwh=0.0,
            total_throughput_kwh=0.0,
            charge_cost=0.0,
            total_charge_kwh=0.0,
            discharge_cost_arr=np.zeros(3),
            battery_discharge_kw=np.zeros(3),
            period_hours=np.full(3, 0.25),
        )
        self.assertAlmostEqual(breakdown["grid_net"], 0.0, places=6)

    def test_degradation_applies_to_total_throughput_not_just_discharge(self):
        # 279.66 kWh throughput at $0.03/kWh -- Mark's own real reported
        # number from issue #149's gist output.
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=[0.0],
            total_cost=0.0,
            degradation_cost_per_kwh=0.03,
            total_throughput_kwh=279.66,
            charge_cost=0.0,
            total_charge_kwh=0.0,
            discharge_cost_arr=np.zeros(1),
            battery_discharge_kw=np.zeros(1),
            period_hours=np.full(1, 0.25),
        )
        self.assertAlmostEqual(breakdown["degradation"], 8.3898, places=3)

    def test_discharge_fee_uses_hour_varying_rate_not_a_flat_scalar(self):
        # Two periods: cheap overnight rate then an expensive peak rate --
        # a flat-scalar shortcut (mean rate * total kWh) would silently
        # get this wrong whenever discharge volume isn't evenly spread
        # across the schedule, exactly the real LocalVolts case this
        # function exists to handle correctly.
        discharge_cost_arr = np.array([0.01, 0.09])
        battery_discharge_kw = np.array([10.0, 2.0])
        period_hours = np.array([1.0, 1.0])
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=[0.0],
            total_cost=0.0,
            degradation_cost_per_kwh=0.0,
            total_throughput_kwh=0.0,
            charge_cost=0.0,
            total_charge_kwh=0.0,
            discharge_cost_arr=discharge_cost_arr,
            battery_discharge_kw=battery_discharge_kw,
            period_hours=period_hours,
        )
        expected = 10.0 * 1.0 * 0.01 + 2.0 * 1.0 * 0.09
        self.assertAlmostEqual(breakdown["discharge_fee"], expected, places=6)
        # A flat-scalar shortcut using the mean rate would give a
        # different (wrong) answer -- assert the two genuinely diverge,
        # so this test would actually fail if someone "simplified" the
        # implementation back to that shortcut.
        wrong_flat_shortcut = (10.0 + 2.0) * np.mean(discharge_cost_arr)
        self.assertNotAlmostEqual(expected, wrong_flat_shortcut, places=2)

    def test_terminal_value_credit_captures_the_real_residual(self):
        # grid_net=4.13, degradation=8.39, charge_fee=1.38,
        # discharge_fee=1.41 -- Mark's own real v0.81.0 numbers from
        # issue #149. total_cost=14.6931 (also his real number) implies
        # a real terminal_value_credit of about -0.61, not zero.
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=[4.1291],
            total_cost=14.6931,
            degradation_cost_per_kwh=0.03,
            total_throughput_kwh=279.66,
            charge_cost=0.01,
            total_charge_kwh=138.48,
            discharge_cost_arr=np.full(1, 0.01),
            battery_discharge_kw=np.full(1, 141.19),
            period_hours=np.full(1, 1.0),
        )
        self.assertAlmostEqual(breakdown["terminal_value_credit"], -0.6225, places=2)
        self.assertNotAlmostEqual(breakdown["terminal_value_credit"], 0.0, places=1)

    def test_none_total_cost_treated_as_zero_not_a_crash(self):
        breakdown = solver_writer.compute_cost_breakdown(
            net_costs=[1.0],
            total_cost=None,
            degradation_cost_per_kwh=0.0,
            total_throughput_kwh=0.0,
            charge_cost=0.0,
            total_charge_kwh=0.0,
            discharge_cost_arr=np.zeros(1),
            battery_discharge_kw=np.zeros(1),
            period_hours=np.full(1, 0.25),
        )
        self.assertAlmostEqual(breakdown["terminal_value_credit"], -1.0, places=6)
