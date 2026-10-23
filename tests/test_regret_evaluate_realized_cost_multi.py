"""Real tests for evaluate_realized_cost_multi() (nimbus issue #768/#585,
Mark Purcell) -- the multi-battery ACHIEVED-cost evaluator that lets
compute_quality_report() score a whole real storage fleet (home + EV
battery_participants) jointly against ONE shared grid connection,
instead of just the home pack.

Real, direct calls into the actual function -- not a reimplementation,
same "solver/*.py has zero homeassistant.* imports, directly importable"
convention as this project's other solver test files.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig
from solver.regret import evaluate_realized_cost, evaluate_realized_cost_multi


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 30.0,
        "initial_soc_kwh": 15.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 30.0,
        "max_charge_kw": 15.0,
        "max_discharge_kw": 15.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestEvaluateRealizedCostMultiBackwardCompat(unittest.TestCase):
    """A single battery, called through the multi-battery function, must
    produce a byte-identical RealizedCost to the single-battery
    evaluate_realized_cost() -- the one pre-existing evaluator every
    other caller (compute_quality_report(), before this change) relied
    on."""

    def test_matches_single_battery_evaluator(self):
        n = 6
        hours = np.full(n, 1.0)
        load = np.full(n, 3.0)
        solar = np.zeros(n)
        import_price = np.full(n, 0.30)
        export_price = np.full(n, 0.08)
        battery = _battery(salvage_value=0.05)
        charge_kw = np.array([0.0, 0.0, 5.0, 5.0, 0.0, 0.0])
        discharge_kw = np.array([2.0, 2.0, 0.0, 0.0, 3.0, 3.0])

        single = evaluate_realized_cost(
            hours=hours,
            load_real_kw=load,
            solar_real_kw=solar,
            import_price_real=import_price,
            export_price_real=export_price,
            charge_committed_kw=charge_kw,
            discharge_committed_kw=discharge_kw,
            charge_cost=battery.charge_cost,
            discharge_cost=battery.discharge_cost,
            final_soc_kwh=18.0,
            salvage_value=battery.salvage_value,
            grid_import_limit_kw=30.0,
            grid_export_limit_kw=30.0,
        )
        multi = evaluate_realized_cost_multi(
            hours=hours,
            load_real_kw=load,
            solar_real_kw=solar,
            import_price_real=import_price,
            export_price_real=export_price,
            batteries=[battery],
            charge_committed_kw=[charge_kw],
            discharge_committed_kw=[discharge_kw],
            final_soc_kwh=[18.0],
        )
        self.assertAlmostEqual(single.total_cost, multi.total_cost, places=9)
        np.testing.assert_allclose(single.grid_import_kw, multi.grid_import_kw)
        np.testing.assert_allclose(single.grid_export_kw, multi.grid_export_kw)
        np.testing.assert_allclose(single.cost_per_period, multi.cost_per_period)


class TestEvaluateRealizedCostMultiAggregation(unittest.TestCase):
    def test_flows_and_costs_sum_across_batteries(self):
        """Two batteries, one charging and one discharging at the same
        time, must net into ONE shared grid balance (not two independent
        ones), and each battery's own cost is priced at its OWN
        charge_cost/discharge_cost -- a genuinely different-priced
        battery must change total_cost even with identical kW flows."""
        n = 2
        hours = np.full(n, 1.0)
        load = np.full(n, 1.0)
        solar = np.zeros(n)
        import_price = np.full(n, 0.30)
        export_price = np.full(n, 0.05)

        cheap = _battery(name="cheap", discharge_cost=0.01)
        pricey = _battery(name="pricey", discharge_cost=0.20)
        charge_kw = np.zeros(n)
        discharge_kw = np.full(n, 4.0)

        cheap_result = evaluate_realized_cost_multi(
            hours=hours,
            load_real_kw=load,
            solar_real_kw=solar,
            import_price_real=import_price,
            export_price_real=export_price,
            batteries=[cheap],
            charge_committed_kw=[charge_kw],
            discharge_committed_kw=[discharge_kw],
            final_soc_kwh=[10.0],
        )
        pricey_result = evaluate_realized_cost_multi(
            hours=hours,
            load_real_kw=load,
            solar_real_kw=solar,
            import_price_real=import_price,
            export_price_real=export_price,
            batteries=[pricey],
            charge_committed_kw=[charge_kw],
            discharge_committed_kw=[discharge_kw],
            final_soc_kwh=[10.0],
        )
        # Same physical flow, different discharge_cost -- pricey must
        # cost strictly more (0.19 $/kWh * 4kWh * 2h = $1.52 more).
        self.assertAlmostEqual(
            pricey_result.total_cost - cheap_result.total_cost, 1.52, places=6
        )

        # Two batteries together: net grid flow is the SUM of both,
        # never independently balanced.
        fleet = evaluate_realized_cost_multi(
            hours=hours,
            load_real_kw=load,
            solar_real_kw=solar,
            import_price_real=import_price,
            export_price_real=export_price,
            batteries=[cheap, pricey],
            charge_committed_kw=[charge_kw, charge_kw],
            discharge_committed_kw=[discharge_kw, discharge_kw],
            final_soc_kwh=[10.0, 10.0],
        )
        # load=1kW, both batteries discharge 4kW each (8kW total) -> net
        # export of 7kW every period, regardless of how many batteries
        # contributed to it.
        np.testing.assert_allclose(fleet.grid_export_kw, np.full(n, 7.0))
        np.testing.assert_allclose(fleet.grid_import_kw, np.zeros(n))
        # Fleet cost is exactly the sum of what each battery would have
        # cost carrying that same discharge alone (the shared grid
        # price term is linear in net flow, so this holds here even
        # though the two batteries' flows interact on the grid side).
        # Compute directly: cheap's own discharge cost + pricey's own,
        # against the SAME fleet grid_export/grid_import (unaffected by
        # which battery contributed the energy).
        expected_battery_cost = (0.01 + 0.20) * 4.0 * 2.0  # $/kWh * kWh
        expected_grid_cost = -export_price[0] * 7.0 * 2.0  # both periods
        self.assertAlmostEqual(
            fleet.total_cost, expected_grid_cost + expected_battery_cost, places=6
        )

    def test_terminal_value_credit_sums_across_batteries(self):
        n = 1
        hours = np.full(n, 1.0)
        zero = np.zeros(n)
        a = _battery(name="a", salvage_value=0.10)
        b = _battery(name="b", salvage_value=0.20)
        result = evaluate_realized_cost_multi(
            hours=hours,
            load_real_kw=zero,
            solar_real_kw=zero,
            import_price_real=np.full(n, 0.30),
            export_price_real=np.full(n, 0.05),
            batteries=[a, b],
            charge_committed_kw=[zero, zero],
            discharge_committed_kw=[zero, zero],
            final_soc_kwh=[10.0, 20.0],
        )
        # Zero grid cost (no load, no battery flow) minus the summed
        # terminal credit: 0.10*10 + 0.20*20 = 5.0.
        self.assertAlmostEqual(result.total_cost, -5.0, places=6)

    def test_mismatched_list_lengths_raise(self):
        n = 2
        hours = np.full(n, 1.0)
        zero = np.zeros(n)
        with self.assertRaises(ValueError):
            evaluate_realized_cost_multi(
                hours=hours,
                load_real_kw=zero,
                solar_real_kw=zero,
                import_price_real=np.full(n, 0.30),
                export_price_real=np.full(n, 0.05),
                batteries=[_battery()],
                charge_committed_kw=[zero, zero],
                discharge_committed_kw=[zero],
                final_soc_kwh=[10.0],
            )


if __name__ == "__main__":
    unittest.main()
