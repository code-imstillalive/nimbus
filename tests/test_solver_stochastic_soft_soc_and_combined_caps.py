"""Regression tests for nimbus issue #354 (Mark Purcell, codebase review),
defects 2 and 3 -- stochastic.py's own combined-direction wash-trade caps
and soft-SoC relaxation had drifted from network.py's own build_plan():

Defect 2 (no soft SoC): BatteryConfig.__post_init__ was relaxed for #328
so `initial_soc_kwh` need only lie in `[0, capacity]` -- network.py handles
a below-floor start via a genuine underfill/overfill relaxation
(soc[t]'s only HARD bound is [0, capacity_kwh]; min_soc_kwh/max_soc_kwh
are a SOFT preference costed via underfill/overfill slack). stochastic.py
still hard-bounded soc{suffix}_t to [min_soc_kwh, max_soc_kwh] directly,
so a below-floor start that couldn't recover in one period made the whole
solve infeasible with no explanation (expected_total_cost=nan, empty
arrays).

Defect 3 (combined-direction caps absent): only the same-period
wash-trade pathways (export funded by solar/discharge; discharge only
drawing on pre-existing SoC) were replicated from network.py. The two
COMBINED-DIRECTION caps -- charge[t]+discharge[t] <= max(max_charge_kw,
max_discharge_kw) (#245) and grid_import[t]+grid_export[t] <=
max(import_limit_kw, export_limit_kw) (#266) -- were missing, so
stochastic.py could still produce the exact simultaneous-import/export or
simultaneous-charge/discharge degeneracy those two issues were opened for
on network.py's own build_plan().

Same "profitable round-trip forces a REAL, economically-motivated
simultaneous nonzero pair, not a degenerate-vertex coin-flip" technique as
this project's own tests/test_solver_combined_direction_cap.py and
tests/test_solver_grid_direction_cap.py (both for build_plan() itself) --
a synthetic scenario engineered so genuine profit, not solver tie-breaking
luck, is what drives the violation being tested for.
"""

from __future__ import annotations

import math
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.stochastic import build_stochastic_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 100.0,
        "initial_soc_kwh": 5.0,
        "min_soc_kwh": 5.0,
        "max_soc_kwh": 100.0,
        "max_charge_kw": 20.0,
        "max_discharge_kw": 20.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestSoftSocRelaxationAllowsRecoveryInsteadOfInfeasible(unittest.TestCase):
    def test_below_floor_initial_soc_does_not_make_the_solve_infeasible(self):
        # min_soc_kwh=20, initial_soc_kwh=2, max_charge_kw=5 -- even a
        # full period of maximum charging cannot reach the floor in one
        # step (2 + 5*0.99 ~= 6.95, still well below 20), so this
        # genuinely exercises the "cannot recover within one period"
        # case, not a trivially-recoverable one.
        battery = _base_battery(
            initial_soc_kwh=2.0,
            min_soc_kwh=20.0,
            max_soc_kwh=100.0,
            max_charge_kw=5.0,
            max_discharge_kw=20.0,
        )
        grid = GridConfig(
            import_price=np.full(3, 0.10),
            export_price=np.zeros(3),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        periods = _flat_grid(3)
        result = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=[np.zeros(3), np.zeros(3)],
            scenario_weights=[0.5, 0.5],
            stochastic_start_period=1,
        )
        self.assertEqual(
            result.status,
            "optimal",
            "a below-floor initial_soc_kwh that can't recover in one "
            "period should schedule real recovery, not go infeasible",
        )
        self.assertTrue(math.isfinite(result.expected_total_cost))


def _combined_cap_scenario(*, n: int = 6, roundtrip_profitable: bool):
    """Mirrors tests/test_solver_combined_direction_cap.py's own proven
    forcing recipe, adapted for build_stochastic_plan()'s raw-array API
    (no LoadConfig/SolarConfig -- those are network.py's own dataclasses).
    All n periods are STAGE 1 except the last (stochastic_start_period
    = n - 1) -- the economics under test all live in stage 1, which is
    built exactly once at weight=1.0, closest to build_plan()'s own
    single-scenario behaviour; both scenarios share identical (zero)
    solar so there's no real hedging story here, just cap enforcement.
    """
    import_price = np.full(n, 0.20)
    export_price = import_price + 0.10 if roundtrip_profitable else import_price - 0.05
    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=40.0,
        export_limit_kw=40.0,
    )
    battery = _base_battery(
        capacity_kwh=40.0,
        initial_soc_kwh=40.0 * 0.5,
        min_soc_kwh=40.0 * 0.05,
        max_soc_kwh=40.0 * 1.0,
        max_charge_kw=21.0,
        max_discharge_kw=24.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.005,
        salvage_value=0.15,
    )
    periods = _flat_grid(n)
    load = np.full(n, 1.5)
    return periods, grid, battery, load


class TestBatteryCombinedDirectionCap(unittest.TestCase):
    def test_combined_cap_always_holds_under_a_profitable_roundtrip(self):
        n = 6
        periods, grid, battery, load = _combined_cap_scenario(
            n=n, roundtrip_profitable=True
        )
        cap = max(battery.max_charge_kw, battery.max_discharge_kw)
        result = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=[np.zeros(n), np.zeros(n)],
            scenario_weights=[0.5, 0.5],
            stochastic_start_period=n - 1,
            load_kw=load,
        )
        self.assertEqual(result.status, "optimal")
        combined = result.stage1_charge_kw + result.stage1_discharge_kw
        self.assertTrue(
            (combined <= cap + 1e-6).all(),
            f"stage-1 combined charge+discharge exceeded {cap}kW: {combined}",
        )
        # Confirms the round-trip was actually genuinely profitable and
        # taken (not a vacuous test where nothing ever moves) -- same
        # "document the real residual, don't assert exclusivity we don't
        # have" honesty as the sibling network.py test suite.
        both_nonzero = [
            (c, d)
            for c, d in zip(
                result.stage1_charge_kw, result.stage1_discharge_kw, strict=True
            )
            if min(c, d) > 1e-3
        ]
        self.assertTrue(
            both_nonzero,
            "expected the profitable round-trip to show real simultaneous "
            "charge+discharge in at least one stage-1 period -- if this "
            "scenario no longer forces it, it needs revisiting before the "
            "cap assertion above means anything",
        )


class TestGridCombinedDirectionCap(unittest.TestCase):
    def test_combined_cap_always_holds_with_a_near_full_battery(self):
        # Same forcing recipe as test_solver_grid_direction_cap.py: cheap
        # import, high export, a near-full battery (discharge never
        # bottoms out against the floor), real solar, zero load.
        n = 6
        import_price = np.full(n, 0.04)
        export_price = np.full(n, 0.33)
        grid = GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=30.0,
            export_limit_kw=30.0,
        )
        battery = _base_battery(
            capacity_kwh=40.0,
            initial_soc_kwh=40.0 * 0.95,
            min_soc_kwh=40.0 * 0.05,
            max_soc_kwh=40.0 * 1.0,
            max_charge_kw=21.0,
            max_discharge_kw=24.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            degradation_cost_per_kwh=0.03,
            salvage_value=0.05,
        )
        periods = _flat_grid(n)
        solar = np.full(n, 13.0)
        cap = max(grid.import_limit_kw, grid.export_limit_kw)

        result = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=[solar, solar],
            scenario_weights=[0.5, 0.5],
            stochastic_start_period=n - 1,
            load_kw=np.zeros(n),
        )
        self.assertEqual(result.status, "optimal")
        combined = result.stage1_grid_import_kw + result.stage1_grid_export_kw
        self.assertTrue(
            (combined <= cap + 1e-6).all(),
            f"stage-1 combined grid_import+grid_export exceeded {cap}kW: {combined}",
        )
        both_nonzero = [
            (gi, ge)
            for gi, ge in zip(
                result.stage1_grid_import_kw,
                result.stage1_grid_export_kw,
                strict=True,
            )
            if min(gi, ge) > 1e-3
        ]
        self.assertTrue(
            both_nonzero,
            "expected the cheap-import/high-export/near-full-battery "
            "scenario to show real simultaneous grid_import+grid_export "
            "in at least one stage-1 period -- if this no longer forces "
            "it, the scenario needs revisiting before the cap assertion "
            "above means anything",
        )
