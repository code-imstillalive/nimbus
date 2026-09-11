"""nimbus issue #732 (Mark Purcell, greenlit fix for #731's own real
cross-battery wash-trade incident: an EV discharging 16.1 kW to charge
the home battery for one period, then reverting, with a real ~5%
round-trip loss and no offsetting benefit -- confirmed live, no
prior-art in EMHASS or HAEO for a multi-battery-participant guard).

The originally-proposed mechanism (a cost attributable to a SPECIFIC
pair of batteries trading with each other) turns out not to be
representable in this LP without a full flow-network redesign -- this
module models one aggregate switchboard balance, not per-pair energy
edges, so attributing which battery's discharge fed which battery's
charge is genuinely unattributable at that level (worked through the
linear algebra directly before implementing anything: a bounded-above
auxiliary "cross-battery transfer" variable with only a cost and no
other constraint always sits at its lower bound of 0, providing zero
real deterrent -- it needs a genuine flow-conservation lower bound to
bind, which this single-bus aggregate model has no way to construct).

The LP-compatible equivalent implemented instead: price each battery's
OWN real round-trip efficiency loss, at that period's own real grid
import price, on every charge/discharge kWh -- not literally
"cross-battery" but a strict generalization that structurally
dominates it. A same-period transfer between two batteries pays this
loss cost TWICE (once on the discharging side, once on the charging
side), scaling with the real, live price the same way #731's own
incident math did. Genuine arbitrage is untouched: it stays worthwhile
exactly when the real price spread between charge-time and
discharge-time exceeds this same loss cost -- the economically correct
condition for arbitrage to be worthwhile in the first place.

Honest scope note (not overclaiming): this raises the real cost of any
wasteful transfer, which directly counters the mechanism #731 exploited,
but it is not a mathematically ironclad guarantee against every possible
solver-level degenerate tie -- #733's own closing comment flagged that
#731's exact incident may be connected to CalibratedOptions' own
secondary-cost handling, a separate, not-yet-fully-diagnosed question.
Per Mark's own stated condition on #732, this still needs verification
against the real household fleet (Sigen home pack + 2 EV participants)
before being called fully done -- these tests prove the mechanism itself
is correctly wired, not that #731's live incident is now impossible.
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


def _periods(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(
        hours=np.full(n, hours), start=datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
    )


def _grid(n: int, import_price: float, export_price: float = -0.01) -> GridConfig:
    return GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, export_price),
        import_limit_kw=200.0,
        export_limit_kw=200.0,
    )


def _battery(
    name: str,
    *,
    initial_soc_kwh: float,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
    salvage_value: float = 0.0,
    max_soc_kwh: float = 40.0,
    must_have_soc_by_period_index: int | None = None,
    must_have_soc_kwh: float | None = None,
) -> BatteryConfig:
    return BatteryConfig(
        name=name,
        capacity_kwh=40.0,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=0.0,
        max_soc_kwh=max_soc_kwh,
        max_charge_kw=20.0,
        max_discharge_kw=20.0,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=salvage_value,
        must_have_soc_by_period_index=must_have_soc_by_period_index,
        must_have_soc_kwh=must_have_soc_kwh,
    )


class TestChargeSideEfficiencyLossPricing(unittest.TestCase):
    def test_charging_cost_includes_real_loss_priced_at_the_real_import_price(self):
        """Force a KNOWN charge amount (a hard SoC floor at the deadline
        makes exactly how much must be charged deterministic, sidestepping
        any need to reason about what the LP would otherwise choose), then
        confirm total_cost matches the OLD terms (charge_cost +
        degradation, both zero degradation here) PLUS the new real
        efficiency-loss term, exactly: import_price * hours *
        (1 - charge_efficiency), for every kWh drawn from the bus to
        charge.
        """
        n = 1
        periods = _periods(n)
        import_price = 0.10
        grid = _grid(n, import_price=import_price)
        charge_efficiency = 0.95
        # Hard floor: must reach exactly 9.5 kWh stored by period 0 (the
        # only period) -- forces charge_kw[0] = 10.0 kW for 1 hour
        # (10 kWh drawn from the bus * 0.95 efficiency = 9.5 kWh stored).
        battery = _battery(
            "home",
            initial_soc_kwh=0.0,
            charge_efficiency=charge_efficiency,
            must_have_soc_by_period_index=0,
            must_have_soc_kwh=9.5,
        )
        loads = [LoadConfig(name="house", forecast_kw=np.zeros(n))]
        solar = SolarConfig(forecast_kw=np.zeros(n))
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        charge_kwh_from_bus = plan.batteries[0].charge_kw[0] * 1.0
        self.assertAlmostEqual(charge_kwh_from_bus, 10.0, places=4)

        # All 10 kWh must come from grid import (no other battery, no
        # solar) -- real grid cost is import_price * 10.
        grid_cost = import_price * 10.0
        # charge_cost (0.01 $/kWh) * 10 kWh drawn from the bus.
        charge_cost_flat = 0.01 * 10.0
        # nimbus issue #732: the new real efficiency-loss term, priced
        # at the real import price -- (1 - 0.95) * 10 kWh lost * $0.10.
        expected_loss_cost = import_price * (1.0 - charge_efficiency) * 10.0
        expected_total = grid_cost + charge_cost_flat + expected_loss_cost
        self.assertAlmostEqual(plan.total_cost, expected_total, places=4)
        # Sanity: the loss term is genuinely nonzero and the dominant
        # reason this differs from the pre-#732 total (grid_cost +
        # charge_cost_flat alone).
        self.assertGreater(expected_loss_cost, 0.0)
        self.assertNotAlmostEqual(
            plan.total_cost, grid_cost + charge_cost_flat, places=4
        )

    def test_higher_real_price_means_a_higher_real_loss_cost_up_to_the_cap(self):
        """The loss cost must genuinely scale with the real, live price
        -- not a flat constant -- confirming this is price-AWARE pricing,
        not just a disguised flat efficiency surcharge. Both prices here
        are chosen to stay under the MIN_CHARGE_DISCHARGE_COST_SPREAD
        cap (see network.py's own #732 comment for why the cap exists)
        so this test proves genuine scaling, not the capped ceiling --
        the cap itself is proven separately below."""
        n = 1
        periods = _periods(n)
        charge_efficiency = 0.90

        def _forced_charge_cost(import_price: float) -> float:
            battery = _battery(
                "home",
                initial_soc_kwh=0.0,
                charge_efficiency=charge_efficiency,
                must_have_soc_by_period_index=0,
                must_have_soc_kwh=4.5,
            )
            plan = build_plan(
                periods=periods,
                grid=_grid(n, import_price=import_price),
                batteries=[battery],
                solar=SolarConfig(forecast_kw=np.zeros(n)),
                loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
            )
            self.assertEqual(plan.status, "optimal")
            return plan.total_cost

        cost_cheap = _forced_charge_cost(0.02)
        cost_expensive = _forced_charge_cost(0.08)
        self.assertGreater(cost_expensive, cost_cheap)
        # 5 kWh drawn from the bus (4.5 / 0.90) either way -- the delta
        # between the two totals must be explained EXACTLY by the real
        # price delta applied to both the direct grid cost AND the new
        # (still-uncapped-at-these-prices) loss term.
        kwh_from_bus = 4.5 / charge_efficiency
        price_delta = 0.08 - 0.02
        expected_delta = price_delta * kwh_from_bus * (1.0 + (1.0 - charge_efficiency))
        self.assertAlmostEqual(cost_expensive - cost_cheap, expected_delta, places=4)

    def test_loss_cost_is_capped_at_the_established_tie_break_magnitude(self):
        """A high real price must NOT scale the loss cost without bound
        -- capped at MIN_CHARGE_DISCHARGE_COST_SPREAD ($0.01/kWh), the
        same lesson #692 already learned for
        battery_charge_earliness_budget_kw (a real regression, caught by
        this project's own full test suite, from an uncapped version of
        this exact mechanism blocking legitimate free-solar charging --
        see network.py's own #732 comment for the full story)."""
        n = 1
        periods = _periods(n)
        charge_efficiency = 0.90
        # Raw (uncapped) loss at this price would be 0.60 * 0.10 = 0.06,
        # six times the $0.01 cap -- if the cap weren't applied, this
        # test's own expected_total below would be wrong by a wide
        # margin, not just imprecise.
        import_price = 0.60
        battery = _battery(
            "home",
            initial_soc_kwh=0.0,
            charge_efficiency=charge_efficiency,
            must_have_soc_by_period_index=0,
            must_have_soc_kwh=4.5,
        )
        plan = build_plan(
            periods=periods,
            grid=_grid(n, import_price=import_price),
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
        )
        self.assertEqual(plan.status, "optimal")
        kwh_from_bus = 4.5 / charge_efficiency
        grid_cost = import_price * kwh_from_bus
        charge_cost_flat = 0.01 * kwh_from_bus
        capped_loss_cost = 0.01 * kwh_from_bus  # the $0.01/kWh cap, not the raw 0.06
        expected_total = grid_cost + charge_cost_flat + capped_loss_cost
        self.assertAlmostEqual(plan.total_cost, expected_total, places=4)


class TestCrossBatteryTransferNowStrictlyCostsMoreThanZeroBenefit(unittest.TestCase):
    def test_a_same_period_ev_to_home_transfer_pays_the_loss_on_both_legs(self):
        """Mirrors #731's own real shape (an EV discharging to charge the
        home battery, same period, real round-trip loss, no other
        constraint forcing it) -- forces the exact transfer via hard SoC
        floors on BOTH batteries (deterministic, not dependent on the
        solver's own tie-break choices) and confirms the real cost
        reflects genuine loss on EACH leg independently: the EV's own
        discharge-side loss AND the home's own charge-side loss, both
        priced at the same real import price, exactly as #732 requires.
        """
        n = 1
        periods = _periods(n)
        import_price = 0.12
        grid = _grid(n, import_price=import_price)
        home_eff = 0.95
        ev_eff = 0.95
        home = _battery(
            "home",
            initial_soc_kwh=0.0,
            charge_efficiency=home_eff,
            must_have_soc_by_period_index=0,
            must_have_soc_kwh=4.75,
        )
        # EV forced to discharge 5 kWh from storage: a tight max_soc_kwh
        # (below initial_soc_kwh) forces it down to that ceiling by the
        # only period, the same technique used elsewhere in this project
        # to force a deterministic amount of movement without relying on
        # the solver's own tie-break choices.
        ev = _battery(
            "ev",
            initial_soc_kwh=30.0,
            charge_efficiency=ev_eff,
            discharge_efficiency=ev_eff,
            max_soc_kwh=25.0,
        )
        loads = [LoadConfig(name="house", forecast_kw=np.zeros(n))]
        solar = SolarConfig(forecast_kw=np.zeros(n))
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[home, ev],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        home_plan = next(b for b in plan.batteries if b.name == "home")
        ev_plan = next(b for b in plan.batteries if b.name == "ev")
        self.assertAlmostEqual(ev_plan.discharge_kw[0], 5.0, places=4)
        self.assertAlmostEqual(home_plan.charge_kw[0], 5.0, places=4)
        # No net grid interaction needed -- EV's 5kW discharge exactly
        # covers home's 5kW charge, zero house load, zero solar.
        self.assertAlmostEqual(plan.grid_import_kw[0], 0.0, places=4)
        self.assertAlmostEqual(plan.grid_export_kw[0], 0.0, places=4)

        flat_costs = 0.01 * 5.0 + 0.01 * 5.0  # discharge_cost + charge_cost
        ev_discharge_loss = import_price * (1.0 / ev_eff - 1.0) * 5.0
        home_charge_loss = import_price * (1.0 - home_eff) * 5.0
        expected_total = flat_costs + ev_discharge_loss + home_charge_loss
        self.assertAlmostEqual(plan.total_cost, expected_total, places=4)
        self.assertGreater(ev_discharge_loss, 0.0)
        self.assertGreater(home_charge_loss, 0.0)


class TestGenuineArbitrageStillWorthwhileWhenPriceSpreadExceedsLoss(unittest.TestCase):
    def test_charging_cheap_and_discharging_expensive_still_profitable(self):
        """The new cost must not make real arbitrage impossible -- a
        large enough real price spread (5c charge, 40c discharge) must
        still comfortably clear the small loss cost this fix adds, same
        as it always would have."""
        n = 2
        periods = _periods(n)
        import_price = np.array([0.05, 0.40])
        grid = GridConfig(
            import_price=import_price,
            export_price=np.array([-0.01, -0.01]),
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        battery = _battery("home", initial_soc_kwh=5.0, salvage_value=0.0)
        loads = [LoadConfig(name="house", forecast_kw=np.array([0.0, 10.0]))]
        solar = SolarConfig(forecast_kw=np.zeros(n))
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        # Cheap-period charging and expensive-period discharging must
        # still both happen -- the arbitrage is not killed by the new
        # loss term.
        self.assertGreater(plan.batteries[0].charge_kw[0], 0.0)
        self.assertGreater(plan.batteries[0].discharge_kw[1], 0.0)


if __name__ == "__main__":
    unittest.main()
