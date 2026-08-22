"""Track A2 -- build_stochastic_plan(), a genuine two-stage stochastic
LP for battery dispatch under solar uncertainty. Deliberately a SEPARATE
module (solver/stochastic.py) from build_plan() -- see that module's
own docstring for the full "why not modify build_plan() directly"
reasoning (700+ lines of real-money-adjacent production code, re-solved
every 5 minutes; the plan itself scoped this as "opt-in, side-by-side
on the shadow-mode chart, never the default until watched").

These tests prove: (1) input validation rejects malformed calls
(too few scenarios, mismatched weight count, weights not summing to
1.0, mismatched solar array length, out-of-range branch point,
scenarios disagreeing before the branch point) with a clear error, not
a silent wrong answer; (2) the REAL point of the feature -- a genuine,
hand-derived economic breakeven where the shared stage-1 decision
flips between "don't hedge" and "hedge fully," driven purely by how
likely the bad scenario is, something a single-scenario deterministic
solve structurally cannot represent at all.

The hedging scenario and its exact breakeven were built iteratively,
against real solved output, not assumed -- two real test-design
mistakes were found and fixed along the way (documented inline at the
scenario's own definition below) before the numbers here were trusted.
"""
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.stochastic import build_stochastic_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = dict(
        capacity_kwh=100.0, initial_soc_kwh=5.0, min_soc_kwh=5.0, max_soc_kwh=100.0,
        max_charge_kw=20.0, max_discharge_kw=20.0, charge_efficiency=0.99, discharge_efficiency=0.99,
        charge_cost=0.01, discharge_cost=0.01, salvage_value=0.0,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


# --- The real hedging scenario, built iteratively against real solved
# output. Two real mistakes found and fixed before trusting this:
#
# Mistake 1: an earlier version gave stage 2 the SAME import and export
# price (both 0.50) -- this makes pre-charging cheap in stage 1 and
# exporting at the expensive stage-2 rate profitable ROUND-TRIP
# ARBITRAGE, completely independent of solar uncertainty. Confirmed
# live: even the SUNNY scenario (which needs no hedge at all -- its own
# solar exactly covers load) was discharging the pre-charged battery
# purely to export it, masking the real hedging signal entirely.
#
# Mistake 2: fixing that by making stage-2 export cheap-but-nonzero
# (0.02) still left a SMALL residual profit motive (0.02 export price -
# 0.01 discharge cost = 0.01/kWh net) to hold and export ANY surplus
# stored energy, in EITHER scenario -- shifting the real breakeven away
# from what a clean hand-derivation predicted (0.1249 hand-derived vs.
# an observed 0.107-0.12 transition). Zero export price removes this
# confound entirely -- the ONLY reason to pre-charge is now genuinely,
# purely, to avoid the cloudy scenario's expensive stage-2 import.
#
# n=4 periods, stochastic_start_period=2. Stage 1 (periods 0-1): no
# solar in either scenario (they must agree there), cheap import
# (0.05/kWh) -- a real, unambiguous opportunity to pre-charge cheaply
# if it turns out to be worth it. Stage 2 (periods 2-3): expensive
# import (0.50/kWh), zero export price, 10kW load in both scenarios.
# "sunny" solar exactly covers stage-2 load (10kW) -- needs no hedge at
# all. "cloudy" has zero solar -- the full 20kWh stage-2 load must be
# covered by either expensive import or a battery pre-charged in stage 1.
def _hedging_grid() -> GridConfig:
    return GridConfig(
        import_price=np.array([0.05, 0.05, 0.50, 0.50]),
        export_price=np.array([0.0, 0.0, 0.0, 0.0]),
        import_limit_kw=50.0, export_limit_kw=50.0,
    )


_LOAD = np.array([0.0, 0.0, 10.0, 10.0])
_SUNNY = np.array([0.0, 0.0, 10.0, 10.0])
_CLOUDY = np.array([0.0, 0.0, 0.0, 0.0])

# Hand-derived: profit per grid-side kWh pre-charged in stage 1, only
# ever useful in the cloudy branch = weight_cloudy * (round-trip
# efficiency * (avoided import price - discharge cost)) - (import price
# paid + charge cost) = weight_cloudy * (0.99*0.99*(0.50-0.01)) - 0.06.
# Breakeven: weight_cloudy = 0.06 / (0.99*0.99*0.49). Confirmed against
# a real solve to exactly this value -- 0.0 precharge just below it,
# the full 20.4061kWh (the exact amount needed to cover cloudy's whole
# 20kWh stage-2 load through round-trip efficiency loss) just above it.
_BREAKEVEN_WEIGHT = 0.06 / (0.99 * 0.99 * (0.50 - 0.01))
_FULL_PRECHARGE_KWH = 20.4061


class TestValidation(unittest.TestCase):
    def _solve(self, **overrides):
        n = 4
        kwargs = dict(
            periods=_flat_grid(n), grid=_hedging_grid(), battery=_base_battery(),
            solar_scenarios=[_SUNNY, _CLOUDY], scenario_weights=[0.5, 0.5],
            stochastic_start_period=2, load_kw=_LOAD,
        )
        kwargs.update(overrides)
        return build_stochastic_plan(**kwargs)

    def test_single_scenario_rejected(self):
        with self.assertRaises(ValueError):
            self._solve(solar_scenarios=[_SUNNY], scenario_weights=[1.0])

    def test_weight_count_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            self._solve(scenario_weights=[0.5, 0.3, 0.2])

    def test_weights_not_summing_to_one_rejected(self):
        with self.assertRaises(ValueError):
            self._solve(scenario_weights=[0.5, 0.4])

    def test_mismatched_solar_array_length_rejected(self):
        with self.assertRaises(ValueError):
            self._solve(solar_scenarios=[_SUNNY, _CLOUDY[:-1]])

    def test_branch_point_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            self._solve(stochastic_start_period=4)  # must be < n, stage 2 needs >=1 period
        with self.assertRaises(ValueError):
            self._solve(stochastic_start_period=-1)

    def test_scenarios_disagreeing_before_branch_point_rejected(self):
        diverges_early = np.array([1.0, 0.0, 0.0, 0.0])  # differs from _SUNNY at t=0, before the branch
        with self.assertRaises(ValueError):
            self._solve(solar_scenarios=[_SUNNY, diverges_early])


class TestGenuineHedgingBehaviour(unittest.TestCase):
    """The real point of the feature: prove the SHARED stage-1 decision
    genuinely flips based on how likely the bad scenario is -- something
    a single-scenario deterministic solve cannot represent at all, since
    it only ever sees one future."""

    def _precharge(self, weight_cloudy: float) -> float:
        plan = build_stochastic_plan(
            periods=_flat_grid(4), grid=_hedging_grid(), battery=_base_battery(),
            solar_scenarios=[_SUNNY, _CLOUDY], scenario_weights=[1.0 - weight_cloudy, weight_cloudy],
            stochastic_start_period=2, load_kw=_LOAD,
        )
        self.assertEqual(plan.status, "optimal")
        return float(sum(plan.stage1_charge_kw))

    def test_no_hedge_below_breakeven(self):
        for w in (0.0, 0.05, 0.10, _BREAKEVEN_WEIGHT - 0.01):
            with self.subTest(weight_cloudy=w):
                self.assertAlmostEqual(self._precharge(w), 0.0, places=3)

    def test_full_hedge_above_breakeven(self):
        for w in (_BREAKEVEN_WEIGHT + 0.01, 0.15, 0.5, 1.0):
            with self.subTest(weight_cloudy=w):
                self.assertAlmostEqual(self._precharge(w), _FULL_PRECHARGE_KWH, places=3)

    def test_stage1_is_a_single_shared_decision_not_per_scenario(self):
        """Structural proof, not just behavioural -- stage1_charge_kw is
        one flat array of length stochastic_start_period, regardless of
        how many scenarios exist, confirming stage 1 is genuinely ONE
        shared set of variables (real two-stage structure), not
        secretly duplicated per scenario."""
        plan = build_stochastic_plan(
            periods=_flat_grid(4), grid=_hedging_grid(), battery=_base_battery(),
            solar_scenarios=[_SUNNY, _CLOUDY], scenario_weights=[0.5, 0.5],
            stochastic_start_period=2, load_kw=_LOAD,
        )
        self.assertEqual(len(plan.stage1_charge_kw), 2)
        self.assertEqual(len(plan.stage2_charge_kw), 2)  # one array per scenario
        for arr in plan.stage2_charge_kw:
            self.assertEqual(len(arr), 2)  # periods 2-3


class TestSalvageValueOnlyAtTrueHorizonEnd(unittest.TestCase):
    """Real bug found and fixed while building this module: salvage_
    value was being applied at stage 1's own boundary period too, an
    INTERMEDIATE point in the real horizon, not a genuine end -- would
    have been a false 'the horizon stops here' credit. Confirms it now
    only ever applies at stage 2's own true final period, per scenario."""

    def test_nonzero_salvage_does_not_create_a_stage1_boundary_credit(self):
        # With salvage_value > 0 and NO real economic reason to pre-
        # charge (weight_cloudy=0, sunny needs no hedge), stage 1 should
        # still charge nothing -- a false boundary credit would show up
        # as spurious pre-charging here, purely to "bank" a salvage
        # credit at the wrong (intermediate) period.
        battery = _base_battery(salvage_value=0.05)
        plan = build_stochastic_plan(
            periods=_flat_grid(4), grid=_hedging_grid(), battery=battery,
            solar_scenarios=[_SUNNY, _CLOUDY], scenario_weights=[1.0, 0.0],
            stochastic_start_period=2, load_kw=_LOAD,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(sum(plan.stage1_charge_kw), 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
