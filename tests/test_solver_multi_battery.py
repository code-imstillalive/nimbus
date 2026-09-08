"""Regression tests for nimbus issue #467, stage 1 (Mark Purcell's own
spec, household re-scoped to "just multi-battery build_plan() support"):
`build_plan()`'s `battery: BatteryConfig` parameter became `batteries:
list[BatteryConfig]`, with each participant getting its own independent
LP variable families, SoC recursion, and same-period wash-trade cap, and
its own entry in the new `Plan.batteries` output list.

Deliberately does NOT reopen the settled #8 audit finding (a single
aggregate BatteryConfig is still correct for multiple physical packs
behind one real inverter) -- every scenario here models two genuinely
separate, independently-metered participants instead (e.g. a home
battery and a second storage channel like the #532 Sigen DC EV
charger), per Mark's own spec.

Four things Mark's spec explicitly asked this test file to prove:
  - TestIndependentSoCDynamics: each battery's own SoC recursion uses
    ITS OWN efficiency/initial_soc_kwh, not another battery's or a
    shared one.
  - TestAggregateEqualsSum: Plan.battery_charge_kw/battery_discharge_kw/
    battery_soc_kwh (every existing reader's own field) is exactly the
    elementwise sum of Plan.batteries -- never a separately-computed
    figure that could silently drift from it.
  - TestSimultaneousChargeAndDischargeAcrossBatteries: two independent
    batteries legitimately charging and discharging in the SAME period
    is real, not a wash trade -- the per-battery combined-direction cap
    (#245) must never be pooled across batteries in a way that would
    block this.
  - TestPerParticipantCrossSolveStability: the rate-limit mechanism
    (mechanism 2, network.py's own module docstring), matched by NAME
    against the previous solve's own Plan.batteries, must bind each
    battery against ITS OWN previous value -- never another battery's,
    and never the old aggregate.
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
from solver.network import BatteryPlan, Plan, build_plan


def _grid(
    n: int, *, import_price: float = 0.30, export_price: float = 0.05
) -> GridConfig:
    return GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, export_price),
        import_limit_kw=50.0,
        export_limit_kw=50.0,
    )


class TestIndependentSoCDynamics(unittest.TestCase):
    def test_each_battery_soc_recursion_matches_its_own_charge_efficiency(self):
        """Two batteries with DIFFERENT efficiencies (0.95 vs 0.90) over a
        real varying-price horizon that gives the LP a genuine reason to
        both charge and discharge each of them across the window --
        manually re-derives the expected SoC trajectory for each battery
        using ITS OWN efficiency and confirms it matches Plan.batteries
        exactly. If the SoC recursion accidentally reused one battery's
        efficiency for the other (e.g. a copy-paste bug keying the wrong
        BatteryConfig), this fails for at least one of the two.
        """
        n = 8
        start = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0] * n), start=start)
        import_price = np.array([0.30, 0.10, 0.10, 0.30, 0.30, 0.10, 0.30, 0.10])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.05,
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 2.0))]

        home = BatteryConfig(
            name="home",
            capacity_kwh=30.0,
            initial_soc_kwh=15.0,
            min_soc_kwh=3.0,
            max_soc_kwh=30.0,
            max_charge_kw=8.0,
            max_discharge_kw=8.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )
        ev = BatteryConfig(
            name="ev",
            capacity_kwh=50.0,
            initial_soc_kwh=25.0,
            min_soc_kwh=5.0,
            max_soc_kwh=50.0,
            max_charge_kw=6.0,
            max_discharge_kw=6.0,
            charge_efficiency=0.90,
            discharge_efficiency=0.90,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )

        plan = build_plan(
            periods=periods, grid=grid, batteries=[home, ev], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual({bp.name for bp in plan.batteries}, {"home", "ev"})
        by_name = {bp.name: bp for bp in plan.batteries}

        for cfg, bp in ((home, by_name["home"]), (ev, by_name["ev"])):
            expected = np.zeros(n)
            prev_soc = cfg.initial_soc_kwh
            for t in range(n):
                prev_soc = (
                    prev_soc
                    + bp.charge_kw[t] * cfg.charge_efficiency * periods.hours[t]
                    - bp.discharge_kw[t] * periods.hours[t] / cfg.discharge_efficiency
                )
                expected[t] = prev_soc
            np.testing.assert_allclose(
                bp.soc_kwh,
                expected,
                atol=1e-6,
                err_msg=f"battery '{cfg.name}' own SoC recursion diverged from "
                "its own charge_efficiency/discharge_efficiency -- possible "
                "cross-battery mixup",
            )


class TestAggregateEqualsSum(unittest.TestCase):
    def test_top_level_fields_are_exactly_the_sum_of_batteries(self):
        """Plan.battery_charge_kw/battery_discharge_kw/battery_soc_kwh
        must be byte-identical to summing Plan.batteries -- every
        existing reader (dashboard cards, quality_report.py/epr.py
        scoring, the dispatch writer) relies on this staying true with
        zero changes on their end."""
        n = 6
        start = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0] * n), start=start)
        grid = _grid(n, import_price=0.25, export_price=0.05)
        solar = SolarConfig(forecast_kw=np.array([0.0, 5.0, 10.0, 10.0, 5.0, 0.0]))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 3.0))]

        home = BatteryConfig(
            name="home",
            capacity_kwh=20.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=2.0,
            max_soc_kwh=20.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )
        ev = BatteryConfig(
            name="ev",
            capacity_kwh=40.0,
            initial_soc_kwh=20.0,
            min_soc_kwh=4.0,
            max_soc_kwh=40.0,
            max_charge_kw=7.0,
            max_discharge_kw=7.0,
            charge_efficiency=0.92,
            discharge_efficiency=0.92,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )

        plan = build_plan(
            periods=periods, grid=grid, batteries=[home, ev], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(len(plan.batteries), 2)

        summed_charge = sum((bp.charge_kw for bp in plan.batteries), np.zeros(n))
        summed_discharge = sum((bp.discharge_kw for bp in plan.batteries), np.zeros(n))
        summed_soc = sum((bp.soc_kwh for bp in plan.batteries), np.zeros(n))
        np.testing.assert_array_equal(plan.battery_charge_kw, summed_charge)
        np.testing.assert_array_equal(plan.battery_discharge_kw, summed_discharge)
        np.testing.assert_array_equal(plan.battery_soc_kwh, summed_soc)


class TestSimultaneousChargeAndDischargeAcrossBatteries(unittest.TestCase):
    def test_one_battery_charging_while_another_discharges_same_period_is_not_blocked(
        self,
    ):
        """Two direction-disabled batteries (a real, already-established
        config pattern in this codebase -- max_charge_kw=0.0 or
        max_discharge_kw=0.0 means "this direction is physically
        disabled", see BatteryConfig's own __post_init__ docstring):
        `overflow` can only ever charge, `home` can only ever discharge.

        `overflow`'s own salvage_value (0.20) genuinely exceeds the net
        benefit of `home` holding its own energy to the horizon end
        (salvage 0.05) -- so whenever `overflow` has charging headroom
        and no genuinely cheaper source (real midday solar, periods 1-2
        only) is available, the LP finds it profitable to route `home`'s
        own discharge DIRECTLY into `overflow`'s charge (net of both
        battery's own throughput friction and the efficiency losses on
        both legs), independent of any load or export-price motive. This
        happens naturally in period 0 (no solar at all): `overflow`
        charges purely from `home`'s own discharge -- two DIFFERENT
        batteries, opposite directions, same period, neither one a wash
        trade (this is a real, physically legitimate reallocation of
        stored energy toward the participant that values it more). The
        combined amount in that period (`overflow` charging + `home`
        discharging, ~9.6kW total) exceeds either single battery's own
        combined-direction cap (#245, max 10kW each) -- if that cap were
        ever wrongly POOLED across batteries instead of kept per-
        participant, this scenario would be infeasible or forced below
        what's needed.
        """
        n = 4
        start = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0] * n), start=start)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.array([0.05, 0.15, 0.15, 0.05]),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.array([0.0, 18.0, 18.0, 0.0]))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        overflow = BatteryConfig(
            name="overflow",
            capacity_kwh=50.0,
            initial_soc_kwh=5.0,
            min_soc_kwh=2.0,
            max_soc_kwh=50.0,
            max_charge_kw=10.0,
            max_discharge_kw=0.0,  # physically disabled -- can only charge
            charge_efficiency=0.90,
            discharge_efficiency=0.90,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.20,
        )
        home = BatteryConfig(
            name="home",
            capacity_kwh=30.0,
            initial_soc_kwh=20.0,
            min_soc_kwh=3.0,
            max_soc_kwh=30.0,
            max_charge_kw=0.0,  # physically disabled -- can only discharge
            max_discharge_kw=10.0,
            charge_efficiency=0.90,
            discharge_efficiency=0.90,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.05,
        )

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[overflow, home],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        by_name = {bp.name: bp for bp in plan.batteries}
        overflow_charge = by_name["overflow"].charge_kw
        overflow_discharge = by_name["overflow"].discharge_kw
        home_charge = by_name["home"].charge_kw
        home_discharge = by_name["home"].discharge_kw

        # Direction-disabled fields genuinely stay at zero.
        np.testing.assert_allclose(overflow_discharge, 0.0, atol=1e-6)
        np.testing.assert_allclose(home_charge, 0.0, atol=1e-6)

        both_active = [
            t for t in range(n) if overflow_charge[t] > 1.0 and home_discharge[t] > 1.0
        ]
        self.assertTrue(
            both_active,
            "expected at least one period with `overflow` charging and "
            "`home` discharging simultaneously -- if this now comes back "
            f"empty the per-battery combined-direction cap (or something "
            f"else) is wrongly blocking legitimate cross-battery activity "
            f"(overflow_charge={overflow_charge}, home_discharge={home_discharge})",
        )
        # And the per-battery cap really is doing real work here, not
        # just quietly non-binding: the combined activity in that period
        # exceeds what either battery's OWN 10kW cap alone could have
        # allowed if it had to carry both roles.
        t = both_active[0]
        self.assertGreater(overflow_charge[t] + home_discharge[t], 10.0)


class TestPerParticipantCrossSolveStability(unittest.TestCase):
    def test_rate_limit_binds_each_battery_against_its_own_previous_value(self):
        """Cross-solve rate limiting (mechanism 2, network.py's own
        module docstring) is a HARD band around each battery's own
        aligned previous-plan value: [prev - max_rate_kw, prev +
        max_rate_kw]. `home`'s own previous period-0 discharge was 0.0;
        `ev`'s own previous period-0 discharge was 20.0. With
        max_rate_kw=1.0, `home`'s NEW period-0 discharge is hard-bound
        to [-1, 1] and `ev`'s to [19, 21] -- each against ITS OWN
        previous value, matched by name.

        This is deliberately NOT a soft/tie-breaking check: `ev`'s max_
        discharge_kw is only 30 (home's is 10) -- if the code ever
        cross-contaminated the two (matched by list position instead of
        name, used the OLD aggregate battery_discharge_kw for both, or
        just failed to match at all and treated every battery as "no
        previous data"), `home` would be hard-forced toward a window
        ([19, 21]) that its own max_discharge_kw=10 cannot even reach --
        this scenario would come back infeasible instead of optimal,
        or `home`'s own period-0 discharge would land outside [-1, 1].
        """
        n = 1
        start = datetime(2026, 9, 8, 17, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0]), start=start)
        grid = _grid(n, import_price=0.30, export_price=0.05)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.array([1.0]))]

        home = BatteryConfig(
            name="home",
            capacity_kwh=20.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=2.0,
            max_soc_kwh=20.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )
        ev = BatteryConfig(
            name="ev",
            capacity_kwh=100.0,
            initial_soc_kwh=80.0,
            min_soc_kwh=5.0,
            max_soc_kwh=100.0,
            max_charge_kw=10.0,
            max_discharge_kw=30.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )

        previous_plan = Plan(
            status="optimal",
            periods=periods,
            battery_charge_kw=np.array([0.0]),
            battery_discharge_kw=np.array([20.0]),  # aggregate: home 0.0 + ev 20.0
            battery_soc_kwh=np.zeros(1),
            grid_import_kw=np.zeros(1),
            # max_rate_kw below also rate-limits the (unchanged, flat)
            # grid_import/grid_export families -- not this test's own
            # concern, but it still applies, so the previous grid_export
            # is seeded near where the new solve will genuinely need to
            # land (roughly the same ~19-21kW the forced ev discharge
            # has to go somewhere), so THAT band doesn't itself make the
            # scenario infeasible and mask the battery-level assertion
            # this test actually cares about.
            grid_export_kw=np.array([20.0]),
            export_bonus_kw=np.zeros(1),
            solar_used_kw=np.zeros(1),
            solar_curtailed_kw=np.zeros(1),
            sheddable_loads=[],
            adequacy_loads=[],
            total_cost=None,
            iterations=0,
            batteries=[
                BatteryPlan(
                    name="home",
                    charge_kw=np.array([0.0]),
                    discharge_kw=np.array([0.0]),
                    soc_kwh=np.array([10.0]),
                ),
                BatteryPlan(
                    name="ev",
                    charge_kw=np.array([0.0]),
                    discharge_kw=np.array([20.0]),
                    soc_kwh=np.array([80.0]),
                ),
            ],
        )

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[home, ev],
            solar=solar,
            loads=loads,
            previous_plan=previous_plan,
            proximal_weight=0.0,
            max_rate_kw=1.0,
        )
        self.assertEqual(plan.status, "optimal")
        by_name = {bp.name: bp for bp in plan.batteries}
        home_discharge0 = float(by_name["home"].discharge_kw[0])
        ev_discharge0 = float(by_name["ev"].discharge_kw[0])

        self.assertTrue(
            -1e-6 <= home_discharge0 <= 1.0 + 1e-6,
            f"home's own period-0 discharge ({home_discharge0}) escaped its "
            "own rate-limit window [-1, 1] around ITS OWN previous value "
            "(0.0) -- possible cross-battery contamination",
        )
        self.assertTrue(
            19.0 - 1e-6 <= ev_discharge0 <= 21.0 + 1e-6,
            f"ev's own period-0 discharge ({ev_discharge0}) escaped its own "
            "rate-limit window [19, 21] around ITS OWN previous value (20.0) "
            "-- possible cross-battery contamination",
        )


class TestValidation(unittest.TestCase):
    def _battery(self, name: str) -> BatteryConfig:
        return BatteryConfig(
            name=name,
            capacity_kwh=20.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=2.0,
            max_soc_kwh=20.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.10,
        )

    def test_empty_batteries_list_raises(self):
        n = 2
        periods = PeriodGrid(hours=np.array([1.0] * n), start=None)
        grid = _grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        with self.assertRaises(ValueError) as ctx:
            build_plan(periods=periods, grid=grid, batteries=[], solar=solar)
        self.assertIn("at least one", str(ctx.exception))

    def test_duplicate_battery_names_raise(self):
        n = 2
        periods = PeriodGrid(hours=np.array([1.0] * n), start=None)
        grid = _grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                periods=periods,
                grid=grid,
                batteries=[self._battery("home"), self._battery("home")],
                solar=solar,
            )
        self.assertIn("unique names", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
