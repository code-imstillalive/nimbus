"""Real tests for oracle_dispatch()'s multi-asset extension (nimbus issue
#768, Option 1) -- exercises the REAL build_plan()/oracle_dispatch()
machinery, not a mock, matching this project's own established
"real functions, real LP" test convention (see test_solver_backtest.py's
own module docstring).

Three things this proves, each a real question #768 itself asked:
1. Backward compatibility -- a single battery/load call produces the
   exact same aggregate trajectory build_plan() itself would, so the one
   pre-existing caller (backtest.py's score_candidate_day()) sees zero
   behaviour change.
2. Genuine multi-battery joint allocation -- two batteries with
   different discharge economics, in the same real price scenario, are
   allocated DIFFERENTLY by the joint solve (the cheaper one carries
   more of the discharge), not split arbitrarily/evenly.
3. Genuine joint battery + controllable-load timing -- an adequacy load
   with a real deadline is scheduled entirely within the real cheap
   price window, not naively as soon as it's allowed to start -- the
   exact "could battery + EV/load timing together have been better"
   question #768's own body poses.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan
from solver.regret import oracle_dispatch


def _periods(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


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


class TestOracleDispatchBackwardCompat(unittest.TestCase):
    """A single battery/load call is byte-identical to build_plan() itself."""

    def test_matches_direct_build_plan_call(self):
        n = 12
        periods = _periods(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=np.full(n, 30.0),
            export_limit_kw=np.full(n, 30.0),
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="house", forecast_kw=np.full(n, 2.0))
        battery = _battery()

        plan = oracle_dispatch(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=[load]
        )
        expected = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=[load]
        )

        self.assertEqual(plan.status, "optimal")
        np.testing.assert_allclose(plan.battery_charge_kw, expected.battery_charge_kw)
        np.testing.assert_allclose(
            plan.battery_discharge_kw, expected.battery_discharge_kw
        )
        np.testing.assert_allclose(plan.battery_soc_kwh, expected.battery_soc_kwh)
        self.assertAlmostEqual(plan.total_cost, expected.total_cost, places=6)

    def test_returns_a_plan_not_a_tuple(self):
        """The old tuple return is gone -- callers read the Plan's own
        fields (see backtest.py's own updated score_candidate_day())."""
        n = 4
        periods = _periods(n)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.05),
            import_limit_kw=np.full(n, 10.0),
            export_limit_kw=np.full(n, 10.0),
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="house", forecast_kw=np.full(n, 1.0))
        plan = oracle_dispatch(
            periods=periods,
            grid=grid,
            batteries=[_battery()],
            solar=solar,
            loads=[load],
        )
        self.assertTrue(hasattr(plan, "battery_charge_kw"))
        self.assertTrue(hasattr(plan, "batteries"))
        self.assertTrue(hasattr(plan, "adequacy_loads"))


class TestOracleDispatchMultiBattery(unittest.TestCase):
    """Two real batteries with different discharge economics -- the joint
    oracle should lean on the cheaper one, not split evenly."""

    def test_cheaper_battery_carries_more_discharge(self):
        n = 6
        periods = _periods(n)
        # Cheap overnight, one real expensive evening peak (period 4-5).
        import_price = np.array([0.10, 0.10, 0.10, 0.10, 0.45, 0.45])
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.02),
            import_limit_kw=np.full(n, 30.0),
            export_limit_kw=np.full(n, 30.0),
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="house", forecast_kw=np.full(n, 5.0))

        cheap_batt = _battery(name="cheap", initial_soc_kwh=10.0, discharge_cost=0.01)
        pricey_batt = _battery(name="pricey", initial_soc_kwh=10.0, discharge_cost=0.20)

        plan = oracle_dispatch(
            periods=periods,
            grid=grid,
            batteries=[cheap_batt, pricey_batt],
            solar=solar,
            loads=[load],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual({b.name for b in plan.batteries}, {"cheap", "pricey"})

        by_name = {b.name: b for b in plan.batteries}
        cheap_discharge_total = float(np.sum(by_name["cheap"].discharge_kw))
        pricey_discharge_total = float(np.sum(by_name["pricey"].discharge_kw))
        # The cheap battery must do at least as much real discharging as
        # the pricier one -- a genuine joint-allocation result, not an
        # artifact of solve order (both batteries are otherwise identical
        # in every respect except discharge_cost).
        self.assertGreater(cheap_discharge_total, pricey_discharge_total)

        # Aggregate top-level fields stay the SUMMED total across both
        # (nimbus #467's own contract) -- not a regression this PR should
        # ever touch, worth a direct assertion here since this test is
        # the first real multi-battery exercise of oracle_dispatch().
        np.testing.assert_allclose(
            plan.battery_discharge_kw,
            by_name["cheap"].discharge_kw + by_name["pricey"].discharge_kw,
        )


class TestOracleDispatchJointLoadTiming(unittest.TestCase):
    """A controllable (adequacy) load with a real deadline, jointly
    re-timed alongside the battery -- directly #768's own question."""

    def test_adequacy_load_scheduled_in_the_cheap_window(self):
        n = 8
        periods = _periods(n)
        # Cheap for the first half, expensive for the second half.
        import_price = np.array([0.05, 0.05, 0.05, 0.05, 0.40, 0.40, 0.40, 0.40])
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.01),
            import_limit_kw=np.full(n, 30.0),
            export_limit_kw=np.full(n, 30.0),
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="house", forecast_kw=np.full(n, 1.0))
        battery = _battery(initial_soc_kwh=5.0, capacity_kwh=15.0, max_soc_kwh=15.0)

        # 4 kWh to deliver, max 2 kW, free to run any time in the whole
        # window -- the LP should choose the cheap first half, not just
        # "as soon as allowed" (period 0 is already allowed, so this
        # isn't a trivial earliest-period tautology).
        controllable_load = AdequacyLoadConfig(
            name="ev",
            max_power_kw=2.0,
            target_kwh=4.0,
            deadline_period=n - 1,
            shortfall_price=10.0,
            earliest_period=0,
        )

        plan = oracle_dispatch(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[load],
            adequacy_loads=[controllable_load],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(len(plan.adequacy_loads), 1)
        ev_plan = plan.adequacy_loads[0]
        self.assertEqual(ev_plan.name, "ev")
        self.assertAlmostEqual(float(ev_plan.shortfall_kwh), 0.0, places=4)

        cheap_half_kwh = float(np.sum(ev_plan.power_kw[:4]))
        expensive_half_kwh = float(np.sum(ev_plan.power_kw[4:]))
        self.assertAlmostEqual(cheap_half_kwh, 4.0, places=4)
        self.assertAlmostEqual(expensive_half_kwh, 0.0, places=4)

    def test_joint_solve_beats_a_naive_isolated_placement(self):
        """The real point of Option 1: free timing must cost meaningfully
        less than a naive, price-blind placement -- isolated from the
        home battery's own compensating behaviour (no battery at all
        here) so the comparison is exactly about the load's own timing,
        not confounded by how the battery reacts to it."""
        n = 8
        periods = _periods(n)
        import_price = np.array([0.05, 0.05, 0.05, 0.05, 0.40, 0.40, 0.40, 0.40])
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.01),
            import_limit_kw=np.full(n, 30.0),
            export_limit_kw=np.full(n, 30.0),
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="house", forecast_kw=np.full(n, 1.0))
        # network.py's own required-convention for "no real battery at
        # all": a BatteryConfig physically disabled via zero charge/
        # discharge power, per build_plan()'s own error message for an
        # empty `batteries` list.
        _disabled_battery = _battery(max_charge_kw=0.0, max_discharge_kw=0.0)

        joint_plan = oracle_dispatch(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery],
            solar=solar,
            loads=[load],
            adequacy_loads=[
                AdequacyLoadConfig(
                    name="ev",
                    max_power_kw=2.0,
                    target_kwh=4.0,
                    deadline_period=n - 1,
                    shortfall_price=10.0,
                    earliest_period=0,
                )
            ],
        )

        # Naive baseline: a household's own uncontrolled charger that
        # simply starts once it's plugged in during the evening (periods
        # 4-5, the expensive window) and runs to completion, with zero
        # price awareness -- forced via earliest_period=4 (deadline stays
        # the full window so the LP has no OTHER choice but periods 4-5,
        # the same "forced-window" construction technique this project's
        # own live household investigations already use, e.g. nimbus
        # #769's own real-data reconstruction, to isolate the real cost
        # of a timing choice).
        forced_evening_plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery],
            solar=solar,
            loads=[load],
            adequacy_loads=[
                AdequacyLoadConfig(
                    name="ev",
                    max_power_kw=2.0,
                    target_kwh=4.0,
                    deadline_period=n - 1,
                    shortfall_price=10.0,
                    earliest_period=4,
                )
            ],
        )
        self.assertEqual(joint_plan.status, "optimal")
        self.assertEqual(forced_evening_plan.status, "optimal")
        # 4 kWh at 0.05 vs 0.40 c/kWh is a real $1.40 gap (plus a few
        # cents from #613's own deliberately tiny earliness tie-break
        # term) -- must show up almost exactly, not just "less than",
        # with no battery in play to absorb or obscure it.
        self.assertAlmostEqual(
            forced_evening_plan.total_cost - joint_plan.total_cost,
            1.40,
            delta=0.05,
        )


if __name__ == "__main__":
    unittest.main()
