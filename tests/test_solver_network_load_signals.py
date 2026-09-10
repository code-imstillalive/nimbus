"""Direct test coverage for nimbus issue #492, Signals 3/7 of #489: per-load
intent bands (`Plan.load_signals`, `LoadSignals`). The second of that issue's
two remaining pieces -- switchboard headroom (`GridSignals.load_headroom_up_
kwh`/`_down_kwh`) already shipped separately.

The two "worked example" style scenarios below are adapted from #492's own
spec text, hand-verified against real solver output before writing the
assertions -- see network.py's own `_load_signal()` docstring for both real
findings that came out of that verification: (1) a sheddable load needs its
shed variable's own ranging MIRRORED into the household-facing served/draw
quantity, not read directly, and (2) classification has to be driven by
reduced cost, not the ranging band's own numeric width (the two only
coincide for a genuinely tied/basic variable). Also: shed_cost is a real,
always-applied $/kWh cost regardless of import price -- a genuine tie needs
shed_cost to EQUAL the serving price, not just "a cheap window" on its own
(a real test-design mistake caught while writing this).

- A 3 kW sheddable load where shed_cost exactly equals the import price:
  serving vs shedding is a genuine tie, so the DEVICE's own draw is reported
  UNLIMIT with band_max ~= its own forecast.
- The same load at a 36c/kWh peak with shed_cost=0.20 (cheaper to shed than
  to serve): the device's own draw is SET to 0, with a POSITIVE reduced cost
  (serving one more kWh there would cost the plan money) -- the raw shed
  variable's own reduced cost is negative in this same scenario, the
  opposite sign, which is exactly why #492's own intent semantic needs the
  mirrored/served framing, not a direct read of the shed variable.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from solver.network import build_plan


def _disabled_battery(n: int) -> BatteryConfig:
    # A battery that can't move at all -- keeps every scenario's own
    # economics isolated to the grid/load side, no battery arbitrage to
    # confound the hand-verified numbers.
    return BatteryConfig(
        name="battery",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=1.0,
        max_soc_kwh=10.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


class TestSheddableLoadUnlimitInACheapWindow(unittest.TestCase):
    def test_free_price_window_reports_unlimit_at_the_forecast_ceiling(self):
        n = 4
        periods = PeriodGrid(
            hours=np.full(n, 1.0), start=datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        )
        # shed_cost is a real, always-applied $/kWh cost regardless of
        # import price -- a genuine tie needs shed_cost to EQUAL the
        # cost of serving instead (import_price), not just a "cheap"
        # price on its own (a real test-design mistake caught while
        # writing this: price=0 with any positive shed_cost is NOT a
        # tie, serving strictly wins since 0 < shed_cost).
        tied_price = 0.18
        grid = GridConfig(
            import_price=np.full(n, tied_price),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        sheddable = SheddableLoadConfig(
            name="pool",
            forecast_kw=np.full(n, 3.0),
            shed_cost=tied_price,
            min_fraction=0.0,
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery(n)],
            solar=solar,
            loads=[],
            sheddable_loads=[sheddable],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(len(plan.load_signals), 1)
        sig = plan.load_signals[0]
        self.assertEqual(sig.name, "pool")
        # shed_cost == import_price -- serving vs shedding is a genuine
        # tie every period, so the device may draw ANYWHERE from 0 up
        # to its own 3 kW forecast without changing the plan's cost.
        self.assertEqual(sig.intent, ["UNLIMIT"] * n)
        np.testing.assert_allclose(sig.band_max_kw, np.full(n, 3.0), atol=1e-4)
        np.testing.assert_allclose(sig.reduced_cost_per_kwh, np.zeros(n), atol=1e-6)


class TestSheddableLoadSetZeroAtAnExpensivePeak(unittest.TestCase):
    def test_shed_cheaper_than_serving_reports_set_zero_with_positive_rc(self):
        n = 4
        periods = PeriodGrid(
            hours=np.full(n, 1.0), start=datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
        )
        # 36c/kWh import, no export/solar -- serving this load costs 0.36
        # $/kWh; shedding it costs only 0.20 $/kWh. Shedding wins outright.
        grid = GridConfig(
            import_price=np.full(n, 0.36),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        sheddable = SheddableLoadConfig(
            name="pool", forecast_kw=np.full(n, 3.0), shed_cost=0.20, min_fraction=0.0
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery(n)],
            solar=solar,
            loads=[],
            sheddable_loads=[sheddable],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        sig = plan.load_signals[0]
        # Shed variable itself is pinned at its own upper bound (fully
        # shed) -- the DEVICE's own draw (served_kw) is therefore pinned
        # at 0, the real-world number the issue's own acceptance
        # criterion calls "SET 0".
        self.assertEqual(sig.intent, ["SET"] * n)
        np.testing.assert_allclose(sig.band_max_kw, np.zeros(n), atol=1e-4)
        for rc in sig.reduced_cost_per_kwh:
            self.assertGreater(
                rc,
                0.0,
                "serving one more kWh here costs the plan money -- the "
                "served-side reduced cost must be positive, the OPPOSITE "
                "sign of the raw shed variable's own reduced cost",
            )


class TestAdequacyLoadIntentUsesTheRawDrawVariableDirectly(unittest.TestCase):
    """An adequacy load's own registered variable already IS the device's
    draw -- no mirroring needed, unlike the sheddable case above."""

    def _scenario(self, *, shortfall_price: float):
        n = 8
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        adequacy = AdequacyLoadConfig(
            name="hws",
            max_power_kw=3.0,
            target_kwh=6.0,
            deadline_period=n - 1,
            earliest_period=0,
            shortfall_price=shortfall_price,
        )
        return build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery(n)],
            solar=solar,
            loads=[],
            adequacy_loads=[adequacy],
            adequacy_semi_continuous=False,
            compute_signals=True,
        )

    def test_flat_price_every_period_ties_on_true_cost(self):
        """Flat import price across the whole horizon: the LP is
        genuinely indifferent to WHICH periods deliver the 6 kWh target,
        as long as the total is met -- every period's own draw variable
        should report a real, non-fabricated band, not necessarily
        UNLIMIT (the deadline constraint itself is a real economic
        factor a period-local view can't see past)."""
        plan = self._scenario(shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE)
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(len(plan.load_signals), 1)
        sig = plan.load_signals[0]
        self.assertEqual(sig.name, "hws")
        self.assertEqual(len(sig.intent), 8)
        for i in sig.intent:
            self.assertIn(i, ("UNLIMIT", "SET"))


class TestBandContainsTheScheduledValue(unittest.TestCase):
    """#492's own acceptance criterion: band_min <= scheduled_kw <=
    band_max on every period of every load -- checked directly against
    the real scheduled value (served_kw for sheddable, power_kw for
    adequacy), not just internally self-consistent."""

    def test_sheddable_served_kw_always_inside_its_own_band(self):
        n = 6
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        # A real price mix -- some periods favor serving, some favor
        # shedding -- so this isn't just another flat-tie scenario.
        grid = GridConfig(
            import_price=np.array([0.10, 0.50, 0.10, 0.50, 0.10, 0.50]),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        sheddable = SheddableLoadConfig(
            name="pool", forecast_kw=np.full(n, 2.5), shed_cost=0.30, min_fraction=0.0
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery(n)],
            solar=solar,
            loads=[],
            sheddable_loads=[sheddable],
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        sig = plan.load_signals[0]
        served_kw = plan.sheddable_loads[0].served_kw
        for t in range(n):
            self.assertLessEqual(
                sig.band_min_kw[t],
                served_kw[t] + 1e-4,
                f"period {t}: band_min {sig.band_min_kw[t]} > scheduled {served_kw[t]}",
            )
            self.assertLessEqual(
                served_kw[t],
                sig.band_max_kw[t] + 1e-4,
                f"period {t}: scheduled {served_kw[t]} > band_max {sig.band_max_kw[t]}",
            )

    def test_adequacy_power_kw_always_inside_its_own_band(self):
        plan = TestAdequacyLoadIntentUsesTheRawDrawVariableDirectly()._scenario(
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE
        )
        sig = plan.load_signals[0]
        power_kw = plan.adequacy_loads[0].power_kw
        for t in range(len(power_kw)):
            self.assertLessEqual(sig.band_min_kw[t], power_kw[t] + 1e-4)
            self.assertLessEqual(power_kw[t], sig.band_max_kw[t] + 1e-4)


class TestZeroWidthBandsAreReportedAsDegenerateNotAConfidentSet(unittest.TestCase):
    def test_a_fully_pinned_zero_ceiling_period_is_flagged_degenerate(self):
        """An adequacy load whose window excludes a period entirely (ub=0
        there via the earliest/deadline bound) has NO real headroom in
        either direction for that period -- degenerate=True, not a
        confident SET with a fabricated non-zero band."""
        n = 4
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.zeros(n),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        # Window is period 0 only -- periods 1-3 are hard-excluded (ub=0).
        adequacy = AdequacyLoadConfig(
            name="hws",
            max_power_kw=3.0,
            target_kwh=1.0,
            deadline_period=0,
            earliest_period=0,
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
        )
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_disabled_battery(n)],
            solar=solar,
            loads=[],
            adequacy_loads=[adequacy],
            adequacy_semi_continuous=False,
            compute_signals=True,
        )
        self.assertEqual(plan.status, "optimal")
        sig = plan.load_signals[0]
        for t in (1, 2, 3):
            self.assertTrue(
                sig.degenerate[t],
                f"period {t} has a hard-zero ceiling (outside the load's "
                "own window) -- must be reported degenerate, not a "
                "confident SET",
            )
            self.assertAlmostEqual(sig.band_max_kw[t], 0.0, places=4)


if __name__ == "__main__":
    unittest.main()
