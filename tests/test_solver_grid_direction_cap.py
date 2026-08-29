"""Regression test for nimbus repo issue #266 (Mark Purcell): LP degeneracy
lets grid_import_kw and grid_export_kw be simultaneously nonzero.

Real-world finding (fixture `purcell_qld1_v0.94.6/`, surfaced building
#264's own seven-flow decomposition): a real captured forecast row showed
`grid_import_kw=13.133` and `grid_export_kw=30.0` in the SAME period --
physically nonsensical (a real household's single grid connection can only
carry current in one direction at any instant), and 24-36 rows out of
~360-365 per fixture showed the same pattern.

Confirmed EMPIRICALLY (not assumed) before writing this fix: replaying the
exact real solar/load/price arrays from BOTH buggy fixtures through this
file's own already-existing (1)-(4) same-period wash-trade constraints
(added 2026-08-16 for issue #245's charge/discharge pathway) still
reproduced the identical class of violation on current `main` -- 25/361 and
36/365 rows respectively. Root cause: (1)+(2) close the pathway where
grid_import[t] funds a FRESH charge-then-discharge round trip within one
period, but do nothing to stop grid_import[t] funding charge[t] while an
entirely separate, ALREADY-EXISTING SoC (accumulated in an earlier period,
so (2)'s own same-period draw restriction never applies to it)
simultaneously discharges to fund grid_export[t] in that same period --
neither leg is a wash trade at the LP-accounting level, but both still hit
the real, single grid connection at once.

Fix (network.py, "SAME-PERIOD WASH-TRADE PREVENTION" section, new
constraint (5), same technique as (3)'s own battery-side cap):
`grid_import[t] + grid_export[t] <= max(import_limit_kw, export_limit_kw)`.
Re-running the same two real fixtures after this fix: 25 -> 9 violating
rows (v0.94.6) and 36 -> 36 violating rows (v0.94.6_midblock, but now
every one capped at exactly the combined limit instead of unbounded) --
a real, measured improvement, NOT a full closure. Same honest framing as
issue #245's own test file: a true `grid_import[t] * grid_export[t] == 0`
complementarity needs a binary per period (MILP, tracked separately as
issue #238) -- this linear cap only bounds the degeneracy budget.

  - test_combined_cap_always_holds: the hard, unconditional guarantee of
    the new constraint, under a scenario engineered to maximise the
    incentive to violate it (cheap import, high export, a near-full
    battery so discharge never bottoms out).
  - test_ordinary_scenario_without_arbitrage_stays_single_direction: the
    common case (export_price <= import_price throughout) -- no residual
    incentive exists, so grid_import and grid_export stay exclusive.
  - test_profitable_configuration_still_shows_partial_grid_direction_violation:
    documents the residual gap directly, using a minimal synthetic
    reproduction of the real fixtures' own shape (solar-heavy midday,
    zero load, near-full battery, a real spread between import and
    export price) -- if this ever starts passing cleanly, the cap may
    have become fully sufficient (or the scenario needs revisiting) and
    this test's own docstring should be updated, not silently deleted.
"""

from __future__ import annotations

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
from solver.network import build_plan

# Matches issue #266's own reported fixture caps (same install as #245).
_IMPORT_LIMIT_KW = 30.0
_EXPORT_LIMIT_KW = 30.0
_COMBINED_CAP = max(_IMPORT_LIMIT_KW, _EXPORT_LIMIT_KW)


def _battery(*, initial_soc_frac: float) -> BatteryConfig:
    capacity = 40.0
    return BatteryConfig(
        capacity_kwh=capacity,
        initial_soc_kwh=capacity * initial_soc_frac,
        min_soc_kwh=capacity * 0.05,
        max_soc_kwh=capacity * 1.0,
        max_charge_kw=21.0,
        max_discharge_kw=24.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        degradation_cost_per_kwh=0.03,
        salvage_value=0.05,
    )


def _grid(import_price: np.ndarray, export_price: np.ndarray) -> GridConfig:
    return GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=_IMPORT_LIMIT_KW,
        export_limit_kw=_EXPORT_LIMIT_KW,
    )


class TestGridDirectionCap(unittest.TestCase):
    def test_combined_cap_always_holds(self):
        """Cheap import (0.04), high export (0.33), a near-full battery
        (95% SoC -- discharge never bottoms out against the floor) and
        real midday solar -- the scenario with maximum real incentive to
        exploit the gap. The new constraint must still hold as a hard
        cap regardless of what the objective wants.
        """
        n = 6
        hours = np.array([1.0] * n)
        periods = PeriodGrid(
            hours=hours, start=datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        )
        grid = _grid(np.full(n, 0.04), np.full(n, 0.33))
        battery = _battery(initial_soc_frac=0.95)
        solar = SolarConfig(forecast_kw=np.full(n, 13.0))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]

        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        combined = plan.grid_import_kw + plan.grid_export_kw
        self.assertTrue(
            (combined <= _COMBINED_CAP + 1e-6).all(),
            f"combined grid_import+grid_export exceeded {_COMBINED_CAP}kW: {combined}",
        )

    def test_ordinary_scenario_without_arbitrage_stays_single_direction(self):
        """The common case: export_price <= import_price throughout (no
        arbitrage available). With normal load/solar and no reason to
        both import and export in the same period, the objective itself
        keeps grid_import and grid_export exclusive.
        """
        n = 24
        hours = np.array([1.0] * n)
        start = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=hours, start=start)
        rng = np.random.default_rng(266)
        import_price = (
            0.20 + 0.05 * np.sin(np.linspace(0, 4 * np.pi, n)) + rng.normal(0, 0.005, n)
        )
        export_price = import_price - 0.05
        grid = _grid(import_price, export_price)
        battery = _battery(initial_soc_frac=0.5)
        solar = SolarConfig(
            forecast_kw=np.clip(8.0 * np.sin(np.linspace(-1, 3, n)), 0.0, None)
        )
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.5))]

        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        for t in range(n):
            gi, ge = plan.grid_import_kw[t], plan.grid_export_kw[t]
            self.assertLess(
                min(gi, ge),
                1e-4,
                f"period {t}: grid_import={gi:.4f} and grid_export={ge:.4f} "
                "both nonzero with no arbitrage available",
            )

    def test_profitable_configuration_still_shows_partial_grid_direction_violation(
        self,
    ):
        """Documents the residual gap: cheap import + high export + a
        near-full battery + real solar makes it genuinely worth both
        continuing to import (to keep charging) and discharging existing,
        already-accumulated SoC to export -- in the SAME period. The
        combined cap bounds it (never more than
        max(import_limit_kw, export_limit_kw) together) but does not
        zero it out. Full elimination needs #238's MILP complementarity.
        """
        n = 6
        hours = np.array([1.0] * n)
        periods = PeriodGrid(
            hours=hours, start=datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        )
        grid = _grid(np.full(n, 0.04), np.full(n, 0.33))
        battery = _battery(initial_soc_frac=0.95)
        solar = SolarConfig(forecast_kw=np.full(n, 13.0))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]

        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        combined = plan.grid_import_kw + plan.grid_export_kw
        # The cap always holds...
        self.assertTrue((combined <= _COMBINED_CAP + 1e-6).all())
        # ...but at least one period still shows real simultaneous
        # nonzero import+export -- the residual gap, not full exclusivity.
        both_nonzero = [
            (gi, ge)
            for gi, ge in zip(plan.grid_import_kw, plan.grid_export_kw)
            if min(gi, ge) > 1e-3
        ]
        self.assertTrue(
            both_nonzero,
            "expected this profitable configuration to still show a "
            "simultaneous grid import+export under the linear-only fix -- "
            "if this now passes cleanly, #266's linear cap may have "
            "become fully sufficient (or the scenario needs revisiting) "
            "and this test's own docstring should be updated",
        )


if __name__ == "__main__":
    unittest.main()
