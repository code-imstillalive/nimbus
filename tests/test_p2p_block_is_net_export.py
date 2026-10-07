"""A P2P block is NET export at the meter: the house is covered on top.

The household's rule (2026-10-07): whatever rate is set in any P2P block is
net to the grid -- "12 kW to the grid, the house on top". `fixed_export_kw`
pinned grid_export[t] (gross) but left grid_import[t] free in the same
period, so whenever the import price fell below what the LP valued stored
battery energy at, it bought the house load from the grid and still counted
the full pinned export. One net meter sees export minus import.

Measured on production 2026-10-07 from 21:11 AEST (buy 18.4c): battery
12.0 kW, plan grid import 1.1 kW alongside grid export 12.0 kW, meter
-10.6 kW. A read-only replay of that solve with v0.94.435's code and with
the household's previous sensors gave the same plan, so neither the release
nor the sensors caused it; capping grid import at 0 in pinned periods put
the battery at 13.2 kW, import 0, net export 12.

The scenario below is that evening's shape: a P2P block with a cheap import
price, then a dearer morning, so stored energy is worth more than buying the
house load now.
"""

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver import p2p_export
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

N = 8
BLOCK = slice(0, 3)  # three pinned hours
LOAD_KW = 1.1


def _plan(
    *,
    fixed: list[float] | None = (12.0, 12.0, 12.0),
    initial_soc_kwh: float = 60.0,
    import_limit_kw: float = 44.0,
):
    import_price = np.array([0.184] * 3 + [0.40] * 5)  # cheap block, dear later
    export_price = np.array([0.09] * N)
    fixed_export_kw = None
    if fixed is not None:
        fixed_export_kw = np.full(N, np.nan)
        fixed_export_kw[: len(fixed)] = fixed
    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=import_limit_kw,
        export_limit_kw=44.0,
        fixed_export_kw=fixed_export_kw,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=122.2,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=122.2 * 0.02,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.9263,
        discharge_efficiency=0.9263,
        charge_cost=0.005,
        discharge_cost=0.005,
        salvage_value=0.30,
    )
    return build_plan(
        periods=PeriodGrid(
            hours=np.ones(N), start=datetime(2026, 10, 7, 11, 0, tzinfo=UTC)
        ),
        grid=grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(N, LOAD_KW))],
    )


class TestABlockIsNetExport(unittest.TestCase):
    def test_the_house_is_covered_on_top_of_the_block(self):
        plan = _plan()
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_allclose(plan.grid_export_kw[BLOCK], 12.0, atol=1e-6)
        np.testing.assert_allclose(plan.grid_import_kw[BLOCK], 0.0, atol=1e-6)
        # battery AC output = 12 to the grid + the house
        delivered = plan.battery_discharge_kw[BLOCK] - plan.battery_charge_kw[BLOCK]
        np.testing.assert_allclose(delivered, 12.0 + LOAD_KW, atol=1e-3)

    def test_every_block_rate_is_net_not_only_twelve(self):
        plan = _plan(fixed=[12.0, 6.0, 3.5])
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_allclose(
            plan.grid_export_kw[BLOCK], [12.0, 6.0, 3.5], atol=1e-6
        )
        np.testing.assert_allclose(plan.grid_import_kw[BLOCK], 0.0, atol=1e-6)

    def test_outside_a_block_import_is_still_free(self):
        plan = _plan()
        # after the block the LP may import as before; the cap is only
        # inside pinned periods
        self.assertEqual(p2p_export.grid_import_ub(5, _grid_for_ub(), 44.0), 44.0)
        self.assertEqual(plan.status, "optimal")

    def test_no_p2p_at_all_is_unchanged(self):
        self.assertEqual(
            p2p_export.grid_import_ub(0, _grid_for_ub(fixed=None), 44.0), 44.0
        )

    def test_enough_for_the_block_but_not_the_house_still_gets_a_plan(self):
        """The case this change could have broken: the battery can deliver
        the block (12 kW x 3 h = 38.9 kWh at 92.63% one-way) but not the
        block plus the house (42.4 kWh). Before, the plan bought the house
        load at the import price; now #390's penalised valve covers the
        shortfall, the plan stays optimal, and it shows as a breach rather
        than as a silent shortfall in net export."""
        plan = _plan(initial_soc_kwh=122.2 * 0.02 + 40.5)
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_allclose(plan.grid_export_kw[BLOCK], 12.0, atol=1e-6)
        self.assertGreater(plan.import_cap_breach_kwh, 0.0)

    def test_not_even_the_block_is_infeasible_exactly_as_before(self):
        """Unchanged by this fix: a battery that cannot deliver the pinned
        export itself was already infeasible (the pin, not the import cap)."""
        plan = _plan(initial_soc_kwh=122.2 * 0.02 + 5.0)
        self.assertEqual(plan.status, "infeasible")

    def test_the_price_spike_override_keeps_its_own_period_0_limit(self):
        self.assertEqual(
            p2p_export.grid_import_ub(0, _grid_for_ub(), 44.0, override_p2p=True), 44.0
        )
        self.assertEqual(p2p_export.grid_import_ub(0, _grid_for_ub(), 44.0), 0.0)


def _grid_for_ub(fixed=(12.0, 12.0, 12.0)):
    fx = None
    if fixed is not None:
        fx = np.full(N, np.nan)
        fx[: len(fixed)] = fixed
    return GridConfig(
        import_price=np.zeros(N),
        export_price=np.zeros(N),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        fixed_export_kw=fx,
    )


if __name__ == "__main__":
    unittest.main()
