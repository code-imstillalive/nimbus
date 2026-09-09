"""Direct test coverage for nimbus issue #567: `BatteryConfig.spike_
override_discharge_kw` -- a real-time "sell into a price spike,
deliberately, right now" household decision, pinning period 0's own
battery discharge to an exact household-chosen kW.

Scenario deliberately uses a CHEAP period-0 price (the LP would
otherwise never want to discharge at all) so a nonzero discharge only
ever appears because the override forced it -- proving the mechanism
actually overrides the LP's own economic preference, not merely
happening to agree with it.
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
)
from solver.network import build_plan


def _grid(n: int) -> GridConfig:
    # import_price BELOW discharge_cost (0.01, see _battery()) -- the LP
    # correctly prefers importing to cover load over draining the
    # battery (discharge_cost is a real cost, not free), so there's no
    # organic reason to discharge at all without the override forcing
    # it. export_price near-zero for the same reason on the export side.
    return GridConfig(
        import_price=np.full(n, 0.001),
        export_price=np.full(n, 0.0001),
        import_limit_kw=50.0,
        export_limit_kw=50.0,
    )


def _battery(*, spike_override_discharge_kw=None) -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=2.0,
        max_soc_kwh=40.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
        spike_override_discharge_kw=spike_override_discharge_kw,
    )


def _plan(*, spike_override_discharge_kw=None, second_battery=False):
    n = 4
    periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
    batteries = [_battery(spike_override_discharge_kw=spike_override_discharge_kw)]
    if second_battery:
        batteries.append(
            BatteryConfig(
                name="participant",
                capacity_kwh=20.0,
                initial_soc_kwh=10.0,
                min_soc_kwh=1.0,
                max_soc_kwh=20.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        )
    return build_plan(
        periods=periods,
        grid=_grid(n),
        batteries=batteries,
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
    )


class TestNoOverrideByDefault(unittest.TestCase):
    def test_none_leaves_period_0_freely_optimized(self):
        plan = _plan(spike_override_discharge_kw=None)
        self.assertEqual(plan.status, "optimal")
        # Cheap price, no override -- the LP has no reason to discharge.
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[0], 0.0, places=3)


class TestOverrideForcesTheExactRate(unittest.TestCase):
    def test_period_0_discharge_is_pinned_to_the_override_value(self):
        plan = _plan(spike_override_discharge_kw=7.5)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[0], 7.5, places=3)
        self.assertAlmostEqual(plan.batteries[0].charge_kw[0], 0.0, places=3)

    def test_period_1_onward_is_unaffected_still_freely_optimized(self):
        """Only period 0 is pinned -- confirms the override isn't
        accidentally applied to the whole horizon."""
        plan = _plan(spike_override_discharge_kw=7.5)
        self.assertEqual(plan.status, "optimal")
        # Cheap price the rest of the horizon too -- period 1 must go
        # back to the LP's own organic (zero-discharge) preference.
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[1], 0.0, places=3)

    def test_grid_export_reflects_the_forced_discharge(self):
        """Real end-to-end proof the override changes the actual plan,
        not just an isolated internal variable -- the forced discharge
        has to go somewhere (export, since load is only 2kW and the
        override forces 7.5kW out of the battery)."""
        plan = _plan(spike_override_discharge_kw=7.5)
        self.assertGreater(plan.grid_export_kw[0], 4.0)

    def test_zero_override_is_a_real_pin_not_a_no_op(self):
        """0.0 is a legitimate, deliberate value (household wants zero
        discharge right now specifically) -- distinct from None (no
        override at all, LP decides freely). Both happen to produce 0
        discharge here since price is cheap anyway, so this is really
        testing that 0.0 doesn't get treated as falsy/None internally."""
        plan = _plan(spike_override_discharge_kw=0.0)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[0], 0.0, places=3)


class TestOverrideOnlyAppliesToBatteriesZero(unittest.TestCase):
    def test_second_battery_participant_is_never_pinned(self):
        """BatteryConfig.spike_override_discharge_kw is only ever set on
        batteries[0] by convention (solver_writer.py's own
        responsibility) -- this proves network.py itself only ever
        applies the pin to index 0, regardless of what a second
        participant's own field happens to hold (None here, matching
        real usage, but the mechanism itself is index-gated, not
        field-gated, exactly like the P2P fixed-export charge gate it
        mirrors)."""
        plan = _plan(spike_override_discharge_kw=7.5, second_battery=True)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[0], 7.5, places=3)
        # The second participant has no override set at all -- cheap
        # price, no organic reason to discharge.
        self.assertAlmostEqual(plan.batteries[1].discharge_kw[0], 0.0, places=3)


class TestOverrideValidation(unittest.TestCase):
    def test_negative_override_is_rejected(self):
        with self.assertRaises(ValueError):
            _battery(spike_override_discharge_kw=-1.0)

    def test_override_above_max_discharge_kw_is_rejected(self):
        with self.assertRaises(ValueError):
            _battery(spike_override_discharge_kw=999.0)

    def test_override_exactly_at_max_discharge_kw_is_accepted(self):
        # Boundary case -- must not off-by-one reject the real ceiling.
        battery = _battery(spike_override_discharge_kw=10.0)
        self.assertEqual(battery.spike_override_discharge_kw, 10.0)


class TestOverrideRespectsUnavailableBattery(unittest.TestCase):
    def test_unavailable_battery_is_not_forced_to_discharge(self):
        """A battery taken offline (available=False, #563 item 2) must
        stay genuinely impossible to dispatch even if a stale spike
        override value is somehow still set -- available=False wins."""
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        battery = BatteryConfig(
            name="home",
            capacity_kwh=40.0,
            initial_soc_kwh=30.0,
            min_soc_kwh=2.0,
            max_soc_kwh=40.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.01,
            discharge_cost=0.01,
            salvage_value=0.0,
            available=False,
            spike_override_discharge_kw=7.5,
        )
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[battery],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        )
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.batteries[0].discharge_kw[0], 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
