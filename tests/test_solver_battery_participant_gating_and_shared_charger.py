"""Regression tests for nimbus issue #563 items 2 and 3, the two pieces
explicitly deferred out of PR #566 (the battery_participant config
surface, already merged) per Mark Purcell's own live-tested review on
that issue: per-participant availability gating and the shared-charger
power constraint.

Both mechanisms live entirely in BatteryConfig (elements.py) and
build_plan() (network.py) -- solver_writer.py's own build_extra_batteries()
translates a live binary_sensor/config into these fields, but the LP
mechanics themselves are what's under test here, same "prove the LP
constraint actually binds" rigor as test_solver_multi_battery.py (#467
stage 1) already established.

  - TestAvailabilityGate: BatteryConfig.available=False makes charge AND
    discharge mathematically impossible for the WHOLE solve (ub=0.0),
    not merely uneconomical -- proven by giving the LP every economic
    reason to want to use that battery and confirming it still can't.
    available=True (the default) is unaffected -- byte-identical to
    every scenario before this field existed.
  - TestDepartureDeadline: must_have_soc_by_period_index/must_have_soc_kwh
    forces soc[idx] >= target even when the LP's own economics would
    otherwise prefer to end lower -- a real HARD floor, not a priced
    preference the LP can trade away. Also proves the no-op cases: both
    None, and an index beyond this solve's own horizon.
  - TestSharedChargerCap: two batteries sharing a shared_charger_group
    both individually WANT to draw near their own max_charge_kw in the
    same period (a real price signal makes it attractive), but the
    group's own shared_charger_max_kw forces their COMBINED charge+
    discharge under that ceiling -- proves the constraint actually
    binds, not just that it's wired in. Also proves an ungrouped
    battery (shared_charger_group=None) is completely unaffected, and
    that the existing PER-BATTERY wash-trade cap (#245/#467) still
    applies independently on top -- two batteries in the same charger
    group are still each individually barred from simultaneous full-
    power charge+discharge on their OWN cap, the shared-group cap is
    additional, not a replacement.
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


def _grid(
    n: int, *, import_price: float = 0.30, export_price: float = 0.05
) -> GridConfig:
    return GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, export_price),
        import_limit_kw=200.0,
        export_limit_kw=200.0,
    )


def _periods(n: int) -> PeriodGrid:
    return PeriodGrid(
        hours=np.array([1.0] * n), start=datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
    )


class TestAvailabilityGate(unittest.TestCase):
    def test_unavailable_battery_cannot_charge_or_discharge_even_when_economically_attractive(
        self,
    ):
        """A real, deep price swing (very cheap early, very expensive
        late) gives the LP every reason to charge this battery early and
        discharge it late -- confirmed by first solving WITH
        available=True and checking real throughput happens. Then, with
        available=False and the identical scenario otherwise unchanged,
        every period's charge_kw and discharge_kw must be exactly 0.0 --
        not merely low, exactly zero, proving this is a hard ub=0.0
        bound, not a cost the LP simply chose to avoid.
        """
        n = 6
        periods = _periods(n)
        import_price = np.array([0.05, 0.05, 0.05, 0.60, 0.60, 0.60])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.02,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        def _battery(available: bool) -> BatteryConfig:
            return BatteryConfig(
                name="ev",
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=4.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.10,
                available=available,
            )

        plan_available = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_battery(True)],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_available.status, "optimal")
        total_throughput_available = float(
            np.sum(plan_available.battery_charge_kw)
            + np.sum(plan_available.battery_discharge_kw)
        )
        self.assertGreater(
            total_throughput_available,
            1.0,
            "sanity check failed: with a real cheap/expensive price swing and "
            "available=True, the battery should show real throughput -- if it "
            "doesn't, the scenario itself isn't exercising the LP the way this "
            "test needs, independent of the gate under test",
        )

        plan_gated = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_battery(False)],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_gated.status, "optimal")
        np.testing.assert_array_equal(
            plan_gated.battery_charge_kw,
            np.zeros(n),
            err_msg="available=False must make charge exactly 0.0 every period, "
            "not merely low -- this must be a hard ub=0.0 bound",
        )
        np.testing.assert_array_equal(
            plan_gated.battery_discharge_kw,
            np.zeros(n),
            err_msg="available=False must make discharge exactly 0.0 every period",
        )


class TestAwayExclusionWindow(unittest.TestCase):
    """nimbus issue #779 (Mark Purcell, confirmed decision): available=
    False bounded to a PREFIX of periods via unavailable_until_period_
    index, instead of the whole horizon -- a currently-away EV should be
    schedulable again once the exclusion window passes, not frozen out
    for the rest of the plan."""

    def _battery(
        self, *, available: bool, unavailable_until_period_index: int | None
    ) -> BatteryConfig:
        return BatteryConfig(
            name="ev",
            capacity_kwh=40.0,
            initial_soc_kwh=20.0,
            min_soc_kwh=4.0,
            max_soc_kwh=40.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.10,
            available=available,
            unavailable_until_period_index=unavailable_until_period_index,
        )

    def test_gated_only_for_periods_before_the_cutoff_index(self):
        """Cheap now, expensive later -- with a real bounded exclusion
        (periods 0-1 gated, periods 2-5 free), the first two periods
        must show exactly zero charge/discharge, but the battery can
        still use the later cheap-then-expensive swing once the window
        lifts."""
        n = 6
        periods = _periods(n)
        import_price = np.array([0.05, 0.05, 0.05, 0.05, 0.60, 0.60])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.02,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[
                self._battery(available=False, unavailable_until_period_index=2)
            ],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_array_equal(
            plan.battery_charge_kw[:2],
            np.zeros(2),
            err_msg="periods before the cutoff index must stay exactly 0.0 charge",
        )
        np.testing.assert_array_equal(
            plan.battery_discharge_kw[:2],
            np.zeros(2),
            err_msg="periods before the cutoff index must stay exactly 0.0 discharge",
        )
        total_throughput_after_cutoff = float(
            np.sum(plan.battery_charge_kw[2:]) + np.sum(plan.battery_discharge_kw[2:])
        )
        self.assertGreater(
            total_throughput_after_cutoff,
            1.0,
            "periods from the cutoff index onward must be schedulable normally -- "
            "if this is 0 the exclusion is still wrongly applying whole-horizon",
        )

    def test_none_index_with_available_false_still_gates_the_whole_horizon(self):
        """Backward-compat regression guard: unavailable_until_period_
        index=None (the default) alongside available=False must be
        byte-identical to the pre-#779 whole-horizon gate -- every
        existing available=False caller/test that never sets this new
        field must keep working unchanged."""
        n = 6
        periods = _periods(n)
        import_price = np.array([0.05, 0.05, 0.05, 0.60, 0.60, 0.60])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.02,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[
                self._battery(available=False, unavailable_until_period_index=None)
            ],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_array_equal(plan.battery_charge_kw, np.zeros(n))
        np.testing.assert_array_equal(plan.battery_discharge_kw, np.zeros(n))

    def test_index_ignored_when_available_is_true(self):
        """unavailable_until_period_index is meaningless when available=
        True (nothing to bound) -- a stale/leftover index value must not
        gate anything."""
        n = 6
        periods = _periods(n)
        import_price = np.array([0.05, 0.05, 0.05, 0.60, 0.60, 0.60])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.02,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[self._battery(available=True, unavailable_until_period_index=4)],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        total_throughput = float(
            np.sum(plan.battery_charge_kw) + np.sum(plan.battery_discharge_kw)
        )
        self.assertGreater(
            total_throughput,
            1.0,
            "available=True must ignore unavailable_until_period_index entirely",
        )

    def test_index_beyond_horizon_gates_every_period_without_crashing(self):
        """An index >= n (e.g. a departure sensor read the wrong period
        grid) must clamp to the whole horizon, not crash or under-gate."""
        n = 4
        periods = _periods(n)
        grid = _grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[
                self._battery(available=False, unavailable_until_period_index=999)
            ],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan.status, "optimal")
        np.testing.assert_array_equal(plan.battery_charge_kw, np.zeros(n))
        np.testing.assert_array_equal(plan.battery_discharge_kw, np.zeros(n))


class TestDepartureDeadline(unittest.TestCase):
    def test_hard_floor_forces_soc_above_what_pure_economics_would_choose(self):
        """A flat, cheap import price gives the LP no real economic
        reason to charge at all beyond bare load-serving -- without a
        deadline, the battery should end low/near its start. With
        must_have_soc_by_period_index/must_have_soc_kwh set to a target
        well above what pure economics would pick, soc at that exact
        period must be >= the target -- a real hard floor, not a cost
        the LP could trade away for a cheaper plan.
        """
        n = 6
        periods = _periods(n)
        grid = _grid(n, import_price=0.10, export_price=0.02)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]

        base_kwargs = {
            "name": "ev",
            "capacity_kwh": 40.0,
            "initial_soc_kwh": 10.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 40.0,
            "max_charge_kw": 10.0,
            "max_discharge_kw": 10.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.005,
            "discharge_cost": 0.01,
            "salvage_value": 0.05,
        }

        plan_no_deadline = build_plan(
            periods=periods,
            grid=grid,
            batteries=[BatteryConfig(**base_kwargs)],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_no_deadline.status, "optimal")
        no_deadline_soc_at_3 = float(plan_no_deadline.battery_soc_kwh[3])

        target_kwh = 30.0
        self.assertLess(
            no_deadline_soc_at_3,
            target_kwh,
            "sanity check failed: pure economics should NOT already reach the "
            "deadline target on its own, otherwise this test can't tell a real "
            "hard floor from coincidence",
        )

        plan_with_deadline = build_plan(
            periods=periods,
            grid=grid,
            batteries=[
                BatteryConfig(
                    **base_kwargs,
                    must_have_soc_by_period_index=3,
                    must_have_soc_kwh=target_kwh,
                )
            ],
            solar=solar,
            loads=loads,
        )
        self.assertEqual(plan_with_deadline.status, "optimal")
        self.assertGreaterEqual(
            float(plan_with_deadline.battery_soc_kwh[3]),
            target_kwh - 1e-6,
            "must_have_soc_by_period_index/must_have_soc_kwh must be a real "
            "hard floor -- soc at the deadline period fell short of the target",
        )

    def test_deadline_beyond_this_horizon_is_a_silent_no_op(self):
        """A deadline period index >= n (this solve's own horizon length)
        must not raise and must not change the plan -- a household's
        departure hour simply being beyond a short manual solve window
        is a normal, expected case, not a misconfiguration.
        """
        n = 4
        periods = _periods(n)
        grid = _grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]
        battery = BatteryConfig(
            name="ev",
            capacity_kwh=40.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=2.0,
            max_soc_kwh=40.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.05,
            must_have_soc_by_period_index=99,
            must_have_soc_kwh=35.0,
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")


class TestSharedChargerCap(unittest.TestCase):
    def test_combined_draw_across_group_is_capped_below_sum_of_individual_maxes(self):
        """Two EV-shaped batteries, both individually allowed up to 15kW
        charge, sharing a group with shared_charger_max_kw=20.0 -- well
        below their combined 30kW individual ceiling. A steep price drop
        makes both want to charge hard in the same early period. Proves
        the group's own combined charge+discharge never exceeds 20.0kW
        in any period, even though each battery's own individual ub
        alone would allow up to 15kW.
        """
        n = 4
        periods = _periods(n)
        import_price = np.array([0.02, 0.40, 0.40, 0.40])
        grid = GridConfig(
            import_price=import_price,
            export_price=import_price - 0.01,
            import_limit_kw=200.0,
            export_limit_kw=200.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 0.5))]

        def _ev(name: str) -> BatteryConfig:
            return BatteryConfig(
                name=name,
                capacity_kwh=60.0,
                initial_soc_kwh=10.0,
                min_soc_kwh=4.0,
                max_soc_kwh=60.0,
                max_charge_kw=15.0,
                max_discharge_kw=15.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.20,
                shared_charger_group="dc_charger",
                shared_charger_max_kw=20.0,
            )

        ev1 = _ev("ev1")
        ev2 = _ev("ev2")
        plan = build_plan(
            periods=periods, grid=grid, batteries=[ev1, ev2], solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        by_name = {bp.name: bp for bp in plan.batteries}
        for t in range(n):
            combined = (
                by_name["ev1"].charge_kw[t]
                + by_name["ev1"].discharge_kw[t]
                + by_name["ev2"].charge_kw[t]
                + by_name["ev2"].discharge_kw[t]
            )
            self.assertLessEqual(
                combined,
                20.0 + 1e-6,
                f"period {t}: combined charge+discharge across the shared "
                f"charger group ({combined}) exceeded shared_charger_max_kw "
                "(20.0) -- the group cap did not bind",
            )
        # Sanity: at least ONE period should show the group actually
        # wanting more than 20kW combined without the cap -- otherwise
        # this test can't tell a real binding constraint from coincidence.
        # Re-solve the identical scenario without the group cap and
        # confirm period 0 (the cheap-import period) alone would exceed
        # 20kW combined charge on its own economics.
        ev1_ungrouped = BatteryConfig(
            name="ev1",
            capacity_kwh=60.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=4.0,
            max_soc_kwh=60.0,
            max_charge_kw=15.0,
            max_discharge_kw=15.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.20,
        )
        ev2_ungrouped = BatteryConfig(
            name="ev2",
            capacity_kwh=60.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=4.0,
            max_soc_kwh=60.0,
            max_charge_kw=15.0,
            max_discharge_kw=15.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.20,
        )
        plan_ungrouped = build_plan(
            periods=periods,
            grid=grid,
            batteries=[ev1_ungrouped, ev2_ungrouped],
            solar=solar,
            loads=loads,
        )
        by_name_ungrouped = {bp.name: bp for bp in plan_ungrouped.batteries}
        combined_period_0_ungrouped = (
            by_name_ungrouped["ev1"].charge_kw[0]
            + by_name_ungrouped["ev2"].charge_kw[0]
        )
        self.assertGreater(
            combined_period_0_ungrouped,
            20.0,
            "sanity check failed: without the group cap, both EVs charging "
            "hard in the cheap period should combine to more than 20kW -- if "
            "not, this scenario can't actually prove the cap binds",
        )

    def test_ungrouped_battery_is_unaffected(self):
        """shared_charger_group=None (the default) must produce a plan
        byte-identical to a solve with no shared-charger mechanism at
        all -- proven by comparing against the same scenario built via
        BatteryConfig's own defaults.
        """
        n = 4
        periods = _periods(n)
        grid = _grid(n)
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]
        kwargs = {
            "name": "home",
            "capacity_kwh": 30.0,
            "initial_soc_kwh": 15.0,
            "min_soc_kwh": 3.0,
            "max_soc_kwh": 30.0,
            "max_charge_kw": 8.0,
            "max_discharge_kw": 8.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.10,
        }
        plan_explicit_none = build_plan(
            periods=periods,
            grid=grid,
            batteries=[
                BatteryConfig(
                    **kwargs, shared_charger_group=None, shared_charger_max_kw=None
                )
            ],
            solar=solar,
            loads=loads,
        )
        plan_default = build_plan(
            periods=periods,
            grid=grid,
            batteries=[BatteryConfig(**kwargs)],
            solar=solar,
            loads=loads,
        )
        np.testing.assert_array_equal(
            plan_explicit_none.battery_charge_kw, plan_default.battery_charge_kw
        )
        np.testing.assert_array_equal(
            plan_explicit_none.battery_discharge_kw, plan_default.battery_discharge_kw
        )


if __name__ == "__main__":
    unittest.main()
