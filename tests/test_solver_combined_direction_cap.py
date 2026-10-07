"""Regression test for nimbus repo issue #245 (Mark Purcell): LP degeneracy
lets battery_charge_kw and battery_discharge_kw be simultaneously nonzero.

Real-world finding (fixture `purcell_qld1_v0.94.6_midblock/`, surfaced via
the #236 price-blend bug): 36 rows showed e.g. charge=17.98 kW alongside
discharge=16.91 kW in the SAME period -- the LP had correctly found the
real net (charge - discharge = -1.06 kW, i.e. charging at 1.06 kW) but the
two per-direction variables had no link between them, leaving a wash-trade
degeneracy budget wide open (a battery has one DC current direction at any
instant; it cannot deliver 17 kW to the AC bus while accepting 18 kW from
it).

Fix (network.py, "SAME-PERIOD WASH-TRADE PREVENTION" section, new
constraint (3)): one linear constraint per period,
`charge[t] + discharge[t] <= max(max_charge_kw, max_discharge_kw)`. This is
NOT a full `charge[t] * discharge[t] == 0` complementarity (that needs a
binary per period -- MILP, tracked separately as issue #238) -- it only
bounds the degeneracy budget.

IMPORTANT correction to issue #245's own claim: the issue asserts "no
incentive exists in the objective for charge > 0 AND discharge > 0" and
that the linear cap is therefore "sufficient in practice." Testing this
directly (what is now test_profitable_roundtrip_window_is_now_exclusive
below) disproved that in general -- whenever a period's export_price
genuinely exceeds its import_price by more than the round-trip loss (the
SAME condition #236 itself notes is a real, legitimate P2P pattern, not a
data artefact: "a REAL household's genuine P2P sale price legitimately,
routinely exceeds import price during its own real 5pm-midnight window"),
importing to charge and discharging to export in the SAME period is a
genuinely profitable round-trip, and the LP takes it up to the new
combined cap. The linear fix still substantially improves on the old
unbounded degeneracy (worst case shrinks from "as large as both individual
caps allow" to "at most max(max_charge_kw, max_discharge_kw) combined"),
and costs nothing when this pricing condition doesn't hold -- but it is a
partial mitigation, not a closure. Full elimination needs #238's MILP
complementarity. This test suite documents both facts honestly rather than
asserting exclusivity outright:

  - test_combined_cap_always_holds: the hard, unconditional guarantee of
    the new constraint itself, checked under a minimal-incentive
    throughput scenario (costs held right at elements.py's own
    DegenerateConfigError floor) -- the case where the objective offers
    the least possible guidance, so the cap is doing all the work.
  - test_ordinary_scenario_without_roundtrip_profit_stays_exclusive: the
    common case (import_price >= export_price throughout, i.e. no
    same-period arbitrage available) -- here the objective's own
    disincentive genuinely does keep charge/discharge exclusive, matching
    what #245 expected.
  - test_profitable_roundtrip_window_is_now_exclusive: the residual gap
    above, which the cap alone bounded but did not eliminate, is now closed
    by nimbus #238's direction binaries; the published plan is one-way.
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

# Matches issue #245's own reported fixture caps.
_MAX_CHARGE_KW = 21.0
_MAX_DISCHARGE_KW = 24.0
_COMBINED_CAP = max(_MAX_CHARGE_KW, _MAX_DISCHARGE_KW)


def _scenario(*, charge_cost: float, discharge_cost: float, roundtrip_profitable: bool):
    """24 hourly periods, the fixture's own charge/discharge caps.

    `roundtrip_profitable=True` sets export_price meaningfully above
    import_price every period (a P2P-window-shaped signal, per #236's own
    documented real pattern) -- same-period charge-then-discharge is a
    genuine profitable round-trip here. `False` keeps import_price >=
    export_price throughout (the ordinary, non-arbitrage case) -- no
    same-period incentive exists to move both variables at once.
    """
    n = 24
    start = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
    hours = np.array([1.0] * n)
    periods = PeriodGrid(hours=hours, start=start)

    rng = np.random.default_rng(245)
    import_price = (
        0.20 + 0.05 * np.sin(np.linspace(0, 4 * np.pi, n)) + rng.normal(0, 0.005, n)
    )
    if roundtrip_profitable:
        # Export comfortably above import even after round-trip losses
        # (efficiency 0.975 each way, throughput costs below) -- the real
        # "genuine P2P sale price exceeds import price" shape #236 found live.
        export_price = import_price + 0.10
    else:
        export_price = import_price - 0.05

    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=40.0,
        export_limit_kw=40.0,
    )
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=40.0,
        initial_soc_kwh=40.0 * 0.5,
        min_soc_kwh=40.0 * 0.05,
        max_soc_kwh=40.0 * 1.0,
        max_charge_kw=_MAX_CHARGE_KW,
        max_discharge_kw=_MAX_DISCHARGE_KW,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost,
        salvage_value=0.15,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.5))]
    return periods, grid, battery, solar, loads


class TestCombinedDirectionCap(unittest.TestCase):
    def test_combined_cap_always_holds(self):
        """Throughput costs held right at elements.py's own
        DegenerateConfigError floor (MIN_CHARGE_DISCHARGE_COST_SPREAD =
        0.01 total) -- the least incentive the config layer allows the
        objective to offer. Even in the profitable-round-trip price shape,
        the new combined-direction constraint must still hold as a hard
        cap regardless of what the objective wants.
        """
        periods, grid, battery, solar, loads = _scenario(
            charge_cost=0.005, discharge_cost=0.005, roundtrip_profitable=True
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        combined = plan.battery_charge_kw + plan.battery_discharge_kw
        self.assertTrue(
            (combined <= _COMBINED_CAP + 1e-6).all(),
            f"combined charge+discharge exceeded {_COMBINED_CAP}kW: {combined}",
        )

    def test_ordinary_scenario_without_roundtrip_profit_stays_exclusive(self):
        """The common case: import_price >= export_price throughout (no
        same-period arbitrage available). With normal positive throughput
        costs (matching test_solver_fixed_export.py's own 0.005/0.01), the
        objective itself has no reason to move both charge and discharge
        at once -- this is the case #245 describes, and it holds.
        """
        periods, grid, battery, solar, loads = _scenario(
            charge_cost=0.005, discharge_cost=0.01, roundtrip_profitable=False
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        for t in range(len(plan.battery_charge_kw)):
            c, d = plan.battery_charge_kw[t], plan.battery_discharge_kw[t]
            self.assertLess(
                min(c, d),
                1e-4,
                f"period {t}: charge={c:.4f} and discharge={d:.4f} both "
                "nonzero with no round-trip arbitrage available",
            )

    def test_profitable_roundtrip_window_is_now_exclusive(self):
        """Was `..._still_shows_partial_wash_trade`, documenting the residual
        gap: with export_price above import_price by more than the round-trip
        loss, the linear cap bounded but did not zero same-period charge and
        discharge. nimbus #238 closes it: `build_plan()` re-solves with a
        direction binary per period when the first plan has opposing flows,
        so the published plan never charges and discharges one battery in
        the same period. The cap still holds too.
        """
        periods, grid, battery, solar, loads = _scenario(
            charge_cost=0.005, discharge_cost=0.01, roundtrip_profitable=True
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        combined = plan.battery_charge_kw + plan.battery_discharge_kw
        self.assertTrue((combined <= _COMBINED_CAP + 1e-6).all())
        both_nonzero = [
            (c, d)
            for c, d in zip(plan.battery_charge_kw, plan.battery_discharge_kw)
            if min(c, d) > 1e-6
        ]
        self.assertEqual(both_nonzero, [])
        self.assertTrue(plan.direction_exclusivity_enforced)
