"""Mark Purcell's own 9-item Solver audit, item #2: objective
completeness. No persisted test suite existed for this package at all
before this file (2026-08-17) -- everything had only ever been verified
via throwaway scratchpad scripts, deleted after use, never re-run.

Each test here is a real ABLATION: build one small, hand-reasoned real
scenario where a specific objective term (salvage_value, headroom_value,
export_bonus_price/volume, a SheddableLoadConfig's shed_cost) is the
ONLY thing that could plausibly change the LP's own optimal choice
between two real alternatives, then confirm turning that term off
actually flips the real, measured outcome. This directly answers "is
every term in the objective doing real, non-redundant work" -- a term
that never changes any real scenario's outcome would be dead weight
Mark's own audit question is asking about.

Run: `python -m unittest discover -s tests` from the repo root, or
`python tests/test_solver_objective_completeness.py` directly.
"""

import unittest

import _solver_path  # noqa: F401  (sets sys.path so the import below works)
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 20.0,
        "initial_soc_kwh": 10.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 20.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
        "headroom_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestSalvageValue(unittest.TestCase):
    """salvage_value: $/kWh credited for real ENERGY remaining at the
    final period. Real scenario: a flat, unattractive export price
    (below the battery's own real discharge_cost, so discharging to
    export is a real net LOSS every period) means there is zero
    ECONOMIC reason to discharge at all -- with salvage_value=0, the
    LP should be genuinely indifferent about ending with any real
    amount of energy left (never discharges either way, so final SoC
    stays at whatever it started at, regardless of salvage_value).
    A real ablation needs a scenario salvage_value can actually MOVE,
    not one where the answer's already pinned by something else."""

    def test_salvage_value_pulls_final_soc_up_when_economically_marginal(self):
        n = 4
        periods = _flat_grid(n)
        # Real, deliberately MARGINAL scenario: export price exactly
        # matches discharge_cost (0.01), so discharging to export is
        # exactly break-even on its own -- salvage_value is the ONLY
        # real tiebreaker between "hold the energy" and "discharge it".
        grid = GridConfig(
            import_price=np.full(n, 0.50),
            export_price=np.full(n, 0.01),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]

        plan_no_salvage = build_plan(
            periods=periods,
            grid=grid,
            battery=_base_battery(salvage_value=0.0),
            solar=solar,
            loads=loads,
        )
        plan_with_salvage = build_plan(
            periods=periods,
            grid=grid,
            battery=_base_battery(salvage_value=0.50),
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_no_salvage.status, "optimal")
        self.assertEqual(plan_with_salvage.status, "optimal")
        final_soc_no_salvage = float(plan_no_salvage.battery_soc_kwh[-1])
        final_soc_with_salvage = float(plan_with_salvage.battery_soc_kwh[-1])
        self.assertGreater(
            final_soc_with_salvage,
            final_soc_no_salvage,
            "salvage_value should pull the LP toward ending with MORE real energy stored "
            "in an economically-marginal scenario -- if it doesn't, salvage_value is dead weight",
        )


class TestHeadroomValue(unittest.TestCase):
    """headroom_value: the symmetric term for salvage_value -- $/kWh
    credited for real UNUSED CAPACITY (max_soc - final_soc), not
    energy remaining. Real, already-documented finding (Nimbus's own
    CLAUDE.md, 2026-08-16): on a real 6h window, headroom_value=0.05
    (< salvage_value=0.10) gave final_soc=100%, headroom_value=0.15
    (> salvage_value=0.10) flipped ALL THE WAY to final_soc=5% -- a
    real, hard-corner (not smooth) response, since this term is
    LINEAR. This test formalizes and PERSISTS that exact already-
    observed real finding as a real regression test."""

    def test_headroom_value_flips_final_soc_to_the_opposite_corner(self):
        n = 6
        periods = _flat_grid(n)
        # No real economic pressure either way (import/export both
        # unattractive relative to battery costs) -- isolates the
        # salvage_value/headroom_value terminal-value tradeoff cleanly.
        grid = GridConfig(
            import_price=np.full(n, 0.50),
            export_price=np.full(n, 0.01),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]

        plan_salvage_wins = build_plan(
            periods=periods,
            grid=grid,
            battery=_base_battery(salvage_value=0.10, headroom_value=0.05),
            solar=solar,
            loads=loads,
        )
        plan_headroom_wins = build_plan(
            periods=periods,
            grid=grid,
            battery=_base_battery(salvage_value=0.10, headroom_value=0.15),
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_salvage_wins.status, "optimal")
        self.assertEqual(plan_headroom_wins.status, "optimal")
        soc_salvage_wins = float(plan_salvage_wins.battery_soc_kwh[-1])
        soc_headroom_wins = float(plan_headroom_wins.battery_soc_kwh[-1])
        self.assertGreater(
            soc_salvage_wins,
            soc_headroom_wins,
            "when salvage_value > headroom_value the LP should end with MORE stored energy; "
            "when headroom_value > salvage_value it should end with LESS -- if the two "
            "settings produce the same final SoC, headroom_value is dead weight",
        )
        # Real corner-behaviour check, reasoned from the scenario's own
        # real economics (not an arbitrary guess): with zero load/solar
        # and import_price (0.50) far above any real terminal credit
        # here, climbing SoC via grid import is never worth it -- the
        # salvage-dominant case can only ever justify PRESERVING the
        # real starting charge (10.0), not climbing toward max_soc.
        # Discharging, by contrast, is genuinely cost-neutral here
        # (export_price == discharge_cost == 0.01, net $0 on the energy
        # itself) -- so headroom_value alone, with nothing opposing it,
        # is real pure profit per kWh freed, and the LP should discharge
        # all the way down to min_soc to collect it.
        self.assertAlmostEqual(
            soc_salvage_wins,
            10.0,
            delta=0.5,
            msg="salvage-dominant case should stay near its real starting SoC, not climb toward max via costly grid import",
        )
        self.assertLess(
            soc_headroom_wins,
            0.2 * 20.0,
            "headroom-dominant case should discharge down near min_soc (cost-neutral energy flow, pure headroom profit)",
        )


class TestExportBonus(unittest.TestCase):
    """export_bonus_price / export_bonus_volume_kwh: the two-tier P2P
    premium mechanism (2026-08-17, real household finding -- see
    elements.py's own GridConfig docstring). Real ablation: with the
    bonus OFF, the LP has zero reason to discharge into a real P2P
    window (base export price alone is unattractive); with it ON, it
    should genuinely capture real bonus volume."""

    def test_export_bonus_creates_real_discharge_incentive_that_did_not_exist_without_it(
        self,
    ):
        n = 4
        periods = _flat_grid(n)
        # Real, deliberately UNATTRACTIVE base rate: 0.005 < discharge_cost
        # (0.01) means discharging to export ALONE is a real net LOSS
        # every period (0.005 - 0.01 = -0.005/kWh) -- a genuine, not
        # merely "small," disincentive, so any real discharge in the
        # "no bonus" baseline can only be explained by the bonus itself
        # missing from that scenario, not a marginal base-rate quirk.
        base_export = np.full(n, 0.005)
        bonus_price = np.array(
            [0.0, 0.40, 0.40, 0.0]
        )  # real premium only in periods 1-2

        grid_no_bonus = GridConfig(
            import_price=np.full(n, 0.50),
            export_price=base_export,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        grid_with_bonus = GridConfig(
            import_price=np.full(n, 0.50),
            export_price=base_export,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            export_bonus_price=bonus_price,
            export_bonus_volume_kwh=10.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]
        battery = _base_battery(initial_soc_kwh=15.0, salvage_value=0.0)

        plan_no_bonus = build_plan(
            periods=periods,
            grid=grid_no_bonus,
            battery=battery,
            solar=solar,
            loads=loads,
        )
        plan_with_bonus = build_plan(
            periods=periods,
            grid=grid_with_bonus,
            battery=battery,
            solar=solar,
            loads=loads,
        )

        self.assertEqual(plan_no_bonus.status, "optimal")
        self.assertEqual(plan_with_bonus.status, "optimal")
        total_export_no_bonus = float(np.sum(plan_no_bonus.grid_export_kw))
        total_export_with_bonus = float(np.sum(plan_with_bonus.grid_export_kw))
        self.assertLess(
            total_export_no_bonus,
            0.01,
            "with no bonus and an unattractive base price, the LP should not export at all",
        )
        self.assertGreater(
            total_export_with_bonus,
            5.0,
            "with a real bonus available, the LP should genuinely discharge to capture it",
        )
        total_bonus_claimed = float(np.sum(plan_with_bonus.export_bonus_kw))
        self.assertGreater(
            total_bonus_claimed,
            5.0,
            "the LP should actually claim real bonus volume, not just export without capturing it",
        )
        self.assertLessEqual(
            total_bonus_claimed,
            10.0 + 1e-6,
            "claimed bonus volume must respect the real cap",
        )


class TestShedCost(unittest.TestCase):
    """SheddableLoadConfig.shed_cost: real ablation using a genuinely
    BINDING import limit -- with shed_cost cheap relative to the real
    cost of importing at the binding limit, the LP should shed real
    load; with shed_cost prohibitively expensive, it should import at
    the limit (or go infeasible on the plain load) instead."""

    def test_shed_cost_determines_whether_the_lp_actually_sheds(self):
        n = 2
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=1.0,
            export_limit_kw=5.0,  # deliberately tight import limit
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=2.0)
        # A real 3kW sheddable load against only a 1kW import limit + a
        # small battery -- genuinely forces a real shed/no-shed choice.
        sheddable_cheap = [
            SheddableLoadConfig(
                name="sheddable",
                forecast_kw=np.full(n, 3.0),
                shed_cost=0.01,
                min_fraction=0.0,
            )
        ]
        sheddable_expensive = [
            SheddableLoadConfig(
                name="sheddable",
                forecast_kw=np.full(n, 3.0),
                shed_cost=50.0,
                min_fraction=0.0,
            )
        ]

        plan_cheap_shed = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            sheddable_loads=sheddable_cheap,
        )
        plan_expensive_shed = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            sheddable_loads=sheddable_expensive,
        )

        self.assertEqual(plan_cheap_shed.status, "optimal")
        self.assertEqual(plan_expensive_shed.status, "optimal")
        total_shed_cheap = float(np.sum(plan_cheap_shed.sheddable_loads[0].shed_kw))
        total_shed_expensive = float(
            np.sum(plan_expensive_shed.sheddable_loads[0].shed_kw)
        )
        self.assertGreater(
            total_shed_cheap,
            total_shed_expensive,
            "a cheap shed_cost should lead to strictly MORE real shedding than an expensive one under the same binding constraint",
        )


if __name__ == "__main__":
    unittest.main()
