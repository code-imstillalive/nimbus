"""SharedCircuitConfig -- a real, physical circuit-headroom cap shared by
two or more AdequacyLoadConfig instances (see its own docstring). Direct
motivation: a real household's two hot-water heaters (HWS L1/L3, each
individually rated 3.7kW) that must never draw simultaneously, but whose
individual AdequacyLoadConfig windows would otherwise be free to overlap
once each is given a genuinely wider window than its own narrow worst-
case slot (the whole point of moving from a fixed clock schedule to an
adequacy-style deadline in the first place).

Confirms the failure mode is real (reproduced first, without the
constraint, under a genuine economic incentive to collide -- not an
artificial/contrived setup) before proving the fix, matching this
project's own established testing discipline for every other stability
mechanism in network.py.
"""

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SharedCircuitConfig,
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
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestSharedCircuitConfigValidation(unittest.TestCase):
    def test_single_member_rejected(self):
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a",), max_combined_power_kw=3.7
            )

    def test_duplicate_member_rejected(self):
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a", "a"), max_combined_power_kw=3.7
            )

    def test_non_positive_cap_rejected(self):
        with self.assertRaises(ValueError):
            SharedCircuitConfig(
                name="c", member_names=("a", "b"), max_combined_power_kw=0.0
            )


class TestSharedCircuitCapReproducesAndFixesRealCollision(unittest.TestCase):
    """Two adequacy loads, overlapping windows, a genuine price signal
    (period 0 far cheaper than period 1) giving the LP a real economic
    reason to put BOTH loads in period 0 if nothing stops it -- exactly
    the real, physical scenario a widened HWS L1/L3 window would create
    without a shared-circuit cap."""

    def _run(self, shared_circuits=None):
        n = 2  # a real 2h window, both loads free to run in either hour
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.array([0.01, 1.00]),  # period 0 far cheaper
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(initial_soc_kwh=2.0, max_discharge_kw=0.5)
        adequacy = [
            AdequacyLoadConfig(
                name="hws_l1",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=1,
            ),
            AdequacyLoadConfig(
                name="hws_l3",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=1,
            ),
        ]
        return build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            shared_circuits=shared_circuits,
        )

    def test_without_the_cap_both_loads_genuinely_collide_at_period_0(self):
        """Sanity check on the test itself: confirm the collision is a
        real, reproducible consequence of the price signal, not assumed.
        """
        plan = self._run(shared_circuits=None)
        self.assertEqual(plan.status, "optimal")
        by_name = {al.name: al for al in plan.adequacy_loads}
        combined_period_0 = (
            by_name["hws_l1"].power_kw[0] + by_name["hws_l3"].power_kw[0]
        )
        self.assertGreater(
            combined_period_0,
            3.7 + 1e-6,
            "without a shared-circuit cap, both loads should genuinely "
            "collide in the cheap period -- if this fails, the test's own "
            "price signal isn't actually reproducing the real problem",
        )

    def test_with_the_cap_combined_power_never_exceeds_the_circuit_limit(self):
        plan = self._run(
            shared_circuits=[
                SharedCircuitConfig(
                    name="hws_circuit",
                    member_names=("hws_l1", "hws_l3"),
                    max_combined_power_kw=3.7,
                )
            ]
        )
        self.assertEqual(plan.status, "optimal")
        by_name = {al.name: al for al in plan.adequacy_loads}
        for t in range(2):
            combined = by_name["hws_l1"].power_kw[t] + by_name["hws_l3"].power_kw[t]
            self.assertLessEqual(
                combined,
                3.7 + 1e-6,
                f"combined HWS power at period {t} exceeds the shared circuit cap",
            )

    def test_with_the_cap_both_targets_are_still_fully_met(self):
        plan = self._run(
            shared_circuits=[
                SharedCircuitConfig(
                    name="hws_circuit",
                    member_names=("hws_l1", "hws_l3"),
                    max_combined_power_kw=3.7,
                )
            ]
        )
        self.assertEqual(plan.status, "optimal")
        for al in plan.adequacy_loads:
            self.assertGreaterEqual(
                al.delivered_by_deadline_kwh,
                3.7 - 1e-6,
                f"'{al.name}' target must still be fully met once sequenced "
                "around the shared cap, not silently under-delivered",
            )


class TestSharedCircuitMissingMemberRaises(unittest.TestCase):
    def test_member_name_not_in_adequacy_loads_raises_clearly(self):
        n = 2
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        adequacy = [
            AdequacyLoadConfig(
                name="hws_l1",
                max_power_kw=3.7,
                target_kwh=1.0,
                earliest_period=0,
                deadline_period=1,
            )
        ]
        shared = [
            SharedCircuitConfig(
                name="hws_circuit",
                # "hws_l3" was never configured -- a real caller mistake.
                member_names=("hws_l1", "hws_l3"),
                max_combined_power_kw=3.7,
            )
        ]
        with self.assertRaises(ValueError) as ctx:
            build_plan(
                periods=periods,
                grid=grid,
                battery=battery,
                solar=solar,
                loads=[],
                adequacy_loads=adequacy,
                shared_circuits=shared,
            )
        self.assertIn("hws_l3", str(ctx.exception))


class TestSharedCircuitGenuineInfeasibility(unittest.TestCase):
    """A shared cap can make two individually-achievable targets jointly
    impossible within an identical, too-narrow shared window -- must
    surface honestly as status="infeasible", same standard as a single
    AdequacyLoadConfig's own genuinely-impossible target."""

    def test_cap_too_low_for_both_loads_single_shared_window_is_infeasible(self):
        n = 1  # both loads have ONLY this one period to run in
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        adequacy = [
            AdequacyLoadConfig(
                name="hws_l1",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=0,
            ),
            AdequacyLoadConfig(
                name="hws_l3",
                max_power_kw=3.7,
                target_kwh=3.7,
                earliest_period=0,
                deadline_period=0,
            ),
        ]
        # Both need the full 3.7kW in the SAME single period to hit their
        # own target -- combined 7.4kW is physically impossible under a
        # 3.7kW shared cap with no other period to move into.
        shared = [
            SharedCircuitConfig(
                name="hws_circuit",
                member_names=("hws_l1", "hws_l3"),
                max_combined_power_kw=3.7,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            shared_circuits=shared,
        )
        self.assertEqual(
            plan.status,
            "infeasible",
            "a shared cap that makes two individually-achievable targets "
            "jointly impossible must surface honestly as infeasible",
        )


class TestSharedCircuitsDefaultIsANoOp(unittest.TestCase):
    def test_none_and_empty_list_produce_identical_plans(self):
        n = 2
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.array([0.05, 0.30]),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery()
        adequacy = [
            AdequacyLoadConfig(
                name="hws_l1",
                max_power_kw=3.7,
                target_kwh=2.0,
                earliest_period=0,
                deadline_period=1,
            )
        ]
        plan_none = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            shared_circuits=None,
        )
        plan_empty = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
            shared_circuits=[],
        )
        self.assertEqual(plan_none.status, plan_empty.status, "optimal")
        self.assertAlmostEqual(plan_none.total_cost, plan_empty.total_cost, places=6)
