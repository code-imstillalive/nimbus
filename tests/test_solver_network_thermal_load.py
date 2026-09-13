"""nimbus issue #774: real LP tests for the hard-constrained thermal load
mechanism (solver/elements.py's ThermalLoadConfig, solver/network.py's
ThermalLoadPlan/build_plan() thermal_loads= support). All against REAL
build_plan() calls -- this project's own established "real functions, real
LP, no mocks" convention (see test_solver_contract_risk.py's own docstring
for the same posture).

Context: the HWS controllable load's "must heat every day" guarantee broke
five separate times (#726, #741, #733, #769, #770/#782), each a bug in a
DIFFERENT one of several loosely-coordinated SOFT mechanisms that jointly
decided "should the tank heat right now" via the pre-existing
AdequacyLoadConfig's own shortfall-priced shape. This mechanism makes the
guarantee a genuine HARD LP constraint instead -- these tests lock in both
halves of that guarantee: the hard case (a reachable target is always met
exactly, never merely biased toward), and the #477-lesson fallback (an
unreachable target relaxes gracefully rather than taking the whole plan
down).
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import (
    DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW,
    _build_plan_once,
    build_plan,
)


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 30.0,
        "initial_soc_kwh": 15.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 30.0,
        "max_charge_kw": 15.0,
        "max_discharge_kw": 15.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.10,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _base_thermal(**overrides) -> ThermalLoadConfig:
    defaults = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 40.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": 23,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.3,
    }
    defaults.update(overrides)
    return ThermalLoadConfig(**defaults)


class TestThermalRecurrenceMatchesHandComputedTrajectory(unittest.TestCase):
    def test_flat_import_price_heats_immediately_then_decays_to_exact_target(self):
        # A flat price gives the LP no cost reason to prefer any period
        # over another -- only the earliness tie-break decides WHEN, so
        # the real trajectory is fully hand-computable: heat as early as
        # possible, then let idle decay run the rest of the way down to
        # land EXACTLY on target_temperature_c at the deadline (the
        # cheapest way to satisfy a hard equality constraint under a
        # tie-break that prefers early action).
        n = 24
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal = _base_thermal()
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
            thermal_loads=[thermal],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.thermal_guarantee_relaxed, [])
        tl = plan.thermal_loads[0]
        self.assertAlmostEqual(tl.temperature_c[-1], 60.0, places=6)
        # Hand-computed: whatever heating happened in period 0/1 plus
        # pure idle decay for the rest must reproduce the exact published
        # trajectory via the same recurrence the LP itself solved with.
        recomputed = thermal.initial_temperature_c
        for t in range(n):
            recomputed += (
                thermal.heating_rate_c_per_kwh * tl.power_kw[t] * periods.hours[t]
                - thermal.idle_decay_c_per_hour * periods.hours[t]
            )
            self.assertAlmostEqual(recomputed, tl.temperature_c[t], places=6)

    def test_initial_temperature_is_a_known_constant_never_a_free_variable(self):
        # A currently-below-floor tank must never make period 0 itself
        # infeasible -- same reasoning BatteryConfig.initial_soc_kwh
        # already gets in the SoC recursion.
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal = _base_thermal(
            initial_temperature_c=10.0,
            target_temperature_c=15.0,
            deadline_period=5,
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.zeros(n))],
            thermal_loads=[thermal],
        )
        self.assertEqual(plan.status, "optimal")


class TestHardDeadlineIsGenuinelyHard(unittest.TestCase):
    def test_unreachable_target_reports_infeasible_before_any_fallback(self):
        # Calling the internal single-solve function directly (thermal_
        # hard_deadline=True, its own default) with a genuinely
        # unreachable target -- the guarantee must be a REAL hard
        # constraint, not silently under-delivered.
        n = 2
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        # Needs (90-20)/8 = 8.75 kWh, only 2h * 1kW = 2 kWh physically
        # possible -- genuinely, structurally unreachable.
        thermal = _base_thermal(
            max_power_kw=1.0,
            initial_temperature_c=20.0,
            target_temperature_c=90.0,
            deadline_period=1,
        )
        result = _build_plan_once(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
            thermal_loads=[thermal],
        )
        self.assertEqual(result.status, "infeasible")

    def test_reachable_target_is_met_exactly_not_merely_biased_toward(self):
        n = 12
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal = _base_thermal(deadline_period=11)
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.zeros(n))],
            thermal_loads=[thermal],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.thermal_loads[0].temperature_c[11], 60.0, places=6)


class TestInfeasibilityFallback(unittest.TestCase):
    def test_unreachable_target_relaxes_gracefully_via_the_public_build_plan(self):
        n = 4
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        thermal = _base_thermal(
            name="hws",
            max_power_kw=1.0,
            initial_temperature_c=20.0,
            target_temperature_c=90.0,
            deadline_period=3,
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
            thermal_loads=[thermal],
        )
        self.assertEqual(
            plan.status,
            "optimal",
            "an unreachable thermal target must never take down the whole plan",
        )
        self.assertEqual(plan.thermal_guarantee_relaxed, ["hws"])
        # Real, honest under-delivery -- not a fabricated success.
        self.assertLess(plan.thermal_loads[0].temperature_c[-1], 90.0)
        # The rest of the plan (the battery) is unaffected -- still a real,
        # sensible dispatch, not corrupted by the relaxation.
        self.assertTrue(np.all(plan.battery_soc_kwh >= 0.0))

    def test_reachable_target_never_triggers_the_fallback(self):
        n = 12
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal = _base_thermal(deadline_period=11)
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.zeros(n))],
            thermal_loads=[thermal],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(plan.thermal_guarantee_relaxed, [])


class TestSoftComfortFloor(unittest.TestCase):
    def test_comfort_floor_triggers_a_real_priced_mid_day_reheat(self):
        n = 12
        periods = _flat_grid(n)
        import_price = np.full(n, 1.00)
        import_price[9:] = 0.05  # cheap only right at the end
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.01),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        load = [LoadConfig(name="load", forecast_kw=np.full(n, 1.0))]
        base_kwargs = dict(
            name="hws",
            max_power_kw=5.0,
            initial_temperature_c=65.0,
            target_temperature_c=60.0,
            earliest_period=0,
            deadline_period=11,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=3.0,
        )

        plan_no_floor = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=load,
            thermal_loads=[ThermalLoadConfig(**base_kwargs)],
        )
        no_floor_min = float(np.min(plan_no_floor.thermal_loads[0].temperature_c))

        plan_floor = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=load,
            thermal_loads=[
                ThermalLoadConfig(
                    **base_kwargs, comfort_floor_c=40.0, comfort_floor_cost=2.0
                )
            ],
        )
        floor_min = float(np.min(plan_floor.thermal_loads[0].temperature_c))

        self.assertLess(
            no_floor_min,
            40.0,
            "the no-floor scenario must genuinely dip below 40 for this test "
            "to actually exercise the floor mechanism",
        )
        self.assertGreaterEqual(
            floor_min,
            40.0 - 1e-6,
            "the comfort floor must genuinely hold the trajectory at or "
            "above 40 degC everywhere",
        )
        self.assertGreater(
            plan_floor.total_cost,
            plan_no_floor.total_cost,
            "a real mid-day reheat must show up as a real, higher dollar cost",
        )

    def test_no_comfort_floor_is_a_complete_no_op(self):
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal_unset = _base_thermal(deadline_period=5)
        thermal_zero_cost = _base_thermal(
            deadline_period=5, comfort_floor_c=30.0, comfort_floor_cost=0.0
        )
        load = [LoadConfig(name="load", forecast_kw=np.zeros(n))]
        plan_unset = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=load,
            thermal_loads=[thermal_unset],
        )
        plan_zero_cost = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=load,
            thermal_loads=[thermal_zero_cost],
        )
        np.testing.assert_allclose(
            plan_unset.thermal_loads[0].temperature_c,
            plan_zero_cost.thermal_loads[0].temperature_c,
        )


class TestEarlinessTieBreak(unittest.TestCase):
    def test_flat_price_nudges_heating_to_the_earliest_feasible_period(self):
        n = 24
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        thermal = _base_thermal()
        self.assertGreater(DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW, 0.0)
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_base_battery()],
            solar=solar,
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
            thermal_loads=[thermal],
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.thermal_loads[0].power_kw
        # All the real heating happens as early as possible -- nothing
        # deferred to a later period once an earlier one is available,
        # under a flat price with no other reason to wait.
        first_zero = next((i for i, p in enumerate(power) if p <= 1e-9), n)
        self.assertTrue(np.all(power[first_zero:] <= 1e-9))


class TestBackwardCompat(unittest.TestCase):
    def test_none_thermal_loads_is_byte_identical_to_omitting_it(self):
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = [LoadConfig(name="load", forecast_kw=np.full(n, 2.0))]
        battery = _base_battery()
        plan_omitted = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=load
        )
        plan_none = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=load,
            thermal_loads=None,
        )
        plan_empty = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=load,
            thermal_loads=[],
        )
        for plan in (plan_none, plan_empty):
            self.assertEqual(plan.thermal_loads, [])
            self.assertEqual(plan.thermal_guarantee_relaxed, [])
            self.assertEqual(plan.total_cost, plan_omitted.total_cost)
            np.testing.assert_allclose(
                plan.battery_soc_kwh, plan_omitted.battery_soc_kwh
            )


if __name__ == "__main__":
    unittest.main()
