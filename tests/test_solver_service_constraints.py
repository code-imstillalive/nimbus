"""Mark Purcell's own 9-item Solver audit, item #5: hard service
constraints. The real concern: AdequacyLoadConfig (hot water reaching a
real target by a real deadline, an EV having enough range by departure
-- see its own docstring, direct response to Mark's own scenario 2) must
not be sacrificed cheaply -- `shortfall_price` (nimbus issue #477) is a
real, high reference cost specifically so a genuinely reachable target
stays met at every real price the grid can throw at it, not something
the LP trades away the moment price gets merely painful.

nimbus issue #477 changed what "genuinely physically impossible" means
for this file: AdequacyLoadConfig's deadline used to be a HARD
constraint (an unreachable target made the whole 96h plan infeasible,
the same failure mode #390 already fixed once for grid_import_excess).
It is now a soft, priced shortfall instead -- Mark's own #477 spec asked
for exactly this ("every requirement is priced slack, not hard-
constrained"). These tests stress AdequacyLoadConfig at real 2x/5x/10x
price multiples and confirm a genuinely reachable target is still fully
met at every multiple (shortfall_price is high enough that the LP
always prefers meeting it), then confirm a genuinely physically-
impossible target now surfaces honestly as status="optimal" with a
real, nonzero shortfall_kwh -- not silently dropped, not a solver-wide
failure, a real priced tradeoff visible in the output.
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
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
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestAdequacyHoldsUnderPriceStress(unittest.TestCase):
    """Real HWS-like scenario: max_power_kw=3.7 (matches this project's
    own real, documented HWS heater draw), target_kwh=5.0 (needs ~1.35h
    of real runtime out of a real 2h window -- genuinely achievable,
    not a hair-trigger edge case), earliest/deadline matching a real
    fixed heating window."""

    def _run_at_import_multiplier(self, multiplier: float):
        n = 4  # a real 2h window (periods 1-2) inside a slightly wider horizon
        periods = _flat_grid(n)
        base_import = 0.30
        grid = GridConfig(
            import_price=np.full(n, base_import * multiplier),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(
            initial_soc_kwh=2.0, max_discharge_kw=1.0
        )  # deliberately weak battery -- forces real grid import to meet the target under stress
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=3.7,
                target_kwh=5.0,
                earliest_period=1,
                deadline_period=2,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        return build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )

    def test_target_is_met_at_normal_price(self):
        plan = self._run_at_import_multiplier(1.0)
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(len(plan.adequacy_loads), 1)
        delivered_kwh = plan.adequacy_loads[0].delivered_by_deadline_kwh
        self.assertGreaterEqual(
            delivered_kwh, 5.0 - 1e-6, "the real target must be met at normal price"
        )

    def test_target_is_still_met_at_2x_5x_10x_price(self):
        for multiplier in (2.0, 5.0, 10.0):
            with self.subTest(multiplier=multiplier):
                plan = self._run_at_import_multiplier(multiplier)
                self.assertEqual(
                    plan.status,
                    "optimal",
                    f"a real, physically-achievable adequacy target must stay satisfiable at {multiplier}x price, not go infeasible just because it's now expensive",
                )
                delivered_kwh = plan.adequacy_loads[0].delivered_by_deadline_kwh
                self.assertGreaterEqual(
                    delivered_kwh,
                    5.0 - 1e-6,
                    f"the real target must STILL be fully met at {multiplier}x price -- "
                    "if it isn't, this is a soft cost term wearing a hard-constraint costume, not a real guarantee",
                )

    def test_cost_genuinely_rises_with_price_even_though_the_target_still_gets_met(
        self,
    ):
        """Real sanity check on the stress test itself: confirm the price
        multiplier is actually biting (real total_cost gets worse), not
        that the scenario is accidentally too cheap to matter at any
        multiplier tested."""
        plan_1x = self._run_at_import_multiplier(1.0)
        plan_10x = self._run_at_import_multiplier(10.0)
        self.assertGreater(
            plan_10x.total_cost,
            plan_1x.total_cost,
            "10x price should produce a real, measurably worse total_cost -- otherwise this stress test isn't actually stressing anything",
        )


class TestAdequacyGenuineShortfall(unittest.TestCase):
    """nimbus issue #477: a target that's PHYSICALLY impossible to reach
    (exceeds max_power_kw * window duration) must surface as a real,
    honest shortfall -- status stays "optimal", shortfall_kwh reports
    exactly how much of target_kwh was genuinely unreachable, and the
    rest of the plan is intact (mirroring #390's grid_import_excess:
    an unsatisfiable requirement costs money and shows up in the output,
    it no longer takes the whole 96h plan down with it).
    """

    def test_physically_impossible_target_reports_a_real_shortfall(self):
        n = 3
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        # 3.7kW max over a real 1-period (1h) window can deliver at most
        # 3.7kWh -- asking for 100kWh is genuinely, physically impossible.
        adequacy = [
            AdequacyLoadConfig(
                name="hws_impossible",
                max_power_kw=3.7,
                target_kwh=100.0,
                earliest_period=0,
                deadline_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(
            plan.status,
            "optimal",
            "a physically-impossible adequacy target must no longer take the whole plan infeasible -- it's a priced shortfall now",
        )
        result = plan.adequacy_loads[0]
        # At most 3.7kWh deliverable (max_power_kw * 1h), so at least
        # 96.3kWh of the 100kWh target is genuinely unreachable.
        self.assertGreaterEqual(
            result.shortfall_kwh,
            96.3 - 1e-6,
            "the shortfall must honestly reflect what was physically undeliverable, not silently absorbed",
        )
        # The deadline constraint (delivered + shortfall >= target_kwh)
        # binds exactly at the optimum -- both delivering power and
        # reporting shortfall cost money, so the LP has no reason to
        # report more of either than the target actually requires.
        self.assertAlmostEqual(
            result.delivered_by_deadline_kwh + result.shortfall_kwh,
            100.0,
            delta=1e-3,
        )


class TestAdequacyValuePerKwhPriceGating(unittest.TestCase):
    """nimbus issue #482 (via #477's own value_per_kwh field): with no
    real deadline pressure beyond a trivially-small target_kwh, a load
    with value_per_kwh set becomes a pure price-gated load -- it runs
    (at max_power_kw) exactly where the switchboard's own marginal cost
    (the power_balance_t{t} dual, here just import_price since solar is
    zero and the battery is disabled so grid import is the only real
    marginal source) is at or below value_per_kwh, and sits idle where
    it's above. Same mechanism HAEO's own consumption_cost uses (see
    AdequacyLoadConfig's own docstring)."""

    def test_load_runs_only_where_marginal_price_is_at_or_below_its_value(self):
        n = 3
        periods = _flat_grid(n)
        # Distinct, well-separated price tiers so 0.10 unambiguously
        # gates period 2 (0.15) out while keeping periods 0/1 (0.03/0.07) in.
        grid = GridConfig(
            import_price=np.array([0.03, 0.07, 0.15]),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        # Battery fully disabled (zero charge/discharge power) so it
        # can't shift demand between periods -- import_price[t] is then
        # exactly the marginal cost of an extra kWh at period t, with
        # nothing else to obscure it.
        battery = _base_battery(max_charge_kw=0.0, max_discharge_kw=0.0)
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
                # Trivially small -- met by a fraction of one cheap
                # period alone, so anything beyond it is pure
                # value_per_kwh-driven demand, not deadline pressure.
                target_kwh=0.5,
                earliest_period=0,
                deadline_period=2,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                value_per_kwh=0.10,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        self.assertAlmostEqual(
            power[0], 5.0, delta=1e-3, msg="0.03 <= 0.10 -- should run at full power"
        )
        self.assertAlmostEqual(
            power[1], 5.0, delta=1e-3, msg="0.07 <= 0.10 -- should run at full power"
        )
        self.assertAlmostEqual(
            power[2], 0.0, delta=1e-3, msg="0.15 > 0.10 -- should sit idle"
        )
