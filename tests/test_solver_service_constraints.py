"""Mark Purcell's own 9-item Solver audit, item #5: hard service
constraints. The real concern: AdequacyLoadConfig (hot water reaching a
real target by a real deadline, an EV having enough range by departure
-- see its own docstring, direct response to Mark's own scenario 2) must
be a genuinely HARD constraint, never something the LP quietly trades
away under real economic pressure. A constraint that's "usually" met but
gets sacrificed the moment price gets painful enough isn't really a
service guarantee at all -- it's just another cost term with a very
large coefficient, which is a real, different, weaker thing.

These tests stress AdequacyLoadConfig at real 2x/5x/10x price multiples
and confirm the target is met at EVERY multiple, then confirm a
genuinely physically-impossible target surfaces honestly as
status="infeasible" (not a silently-adjusted or partially-met target --
see AdequacyLoadConfig's own docstring for why this is the correct,
honest behaviour, not a bug).
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
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
    defaults = dict(
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=20.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
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
            )
        ]
        return build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
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


class TestAdequacyGenuineInfeasibility(unittest.TestCase):
    """The other half of "genuinely hard": a target that's PHYSICALLY
    impossible to reach (exceeds max_power_kw * window duration) must
    surface as a real, honest status="infeasible" -- never a silently
    partial delivery, and never solved by quietly ignoring the deadline.
    """

    def test_physically_impossible_target_is_honestly_infeasible(self):
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
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(
            plan.status,
            "infeasible",
            "a physically-impossible adequacy target must surface honestly as infeasible, not a silently-adjusted or partial outcome",
        )


if __name__ == "__main__":
    unittest.main()
