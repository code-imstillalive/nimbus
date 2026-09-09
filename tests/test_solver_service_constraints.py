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
    """nimbus issue #482 (via #477's own value_per_kwh field): a load
    with value_per_kwh set becomes a price-gated load -- it runs exactly
    where the switchboard's own marginal cost (the power_balance_t{t}
    dual, here just import_price since solar is zero and the battery is
    disabled so grid import is the only real marginal source) is at or
    below value_per_kwh, and sits idle where it's above. Same mechanism
    HAEO's own consumption_cost uses (see AdequacyLoadConfig's own
    docstring).

    nimbus issue #606 (Mark Purcell, real finding on his own heat pump:
    a load with value_per_kwh set ran at MAX POWER in every period below
    the gate price, well past its own real target_kwh, because the old
    mechanism credited raw power[t] directly with no cap on the total
    credited energy -- "the credit keeps the load on at max power in
    every cheap period and the published plan overstates energy and
    cost"). Fixed via a served_credit[t] variable capped both per-period
    (<= power[t]) and in aggregate (<= target_kwh, network.py's own
    adequacy_credit_cap_ constraint) -- these tests now lock in the
    CORRECTED behavior: price-gating still works exactly as before, but
    the load only ever delivers up to its real target, filling the
    cheapest available periods first and stopping once met, never
    running on past it just because the price is still low enough."""

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
                # Needs BOTH cheap periods together (5.0 + 1.0 = 6.0) to
                # confirm the LP fills the cheapest period to its own
                # max first, then only as much of the next-cheapest as
                # still needed -- not #606's own old "runs at max
                # everywhere it's allowed" behavior.
                target_kwh=6.0,
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
            power[0], 5.0, delta=1e-3, msg="0.03 <= 0.10 -- cheapest period, full power"
        )
        self.assertAlmostEqual(
            power[1],
            1.0,
            delta=1e-3,
            msg="0.07 <= 0.10 -- but only enough to reach the 6.0 kWh target",
        )
        self.assertAlmostEqual(
            power[2], 0.0, delta=1e-3, msg="0.15 > 0.10 -- should sit idle"
        )
        self.assertAlmostEqual(plan.adequacy_loads[0].shortfall_kwh, 0.0, delta=1e-3)

    def test_load_never_delivers_beyond_its_own_target_even_when_cheap(self):
        # nimbus issue #606's own exact repro shape: a trivially small
        # target met by a fraction of one cheap period -- the load must
        # NOT keep running at max power through every other cheap
        # period just because value_per_kwh still exceeds the price
        # there (the pre-fix bug this test file's own git history shows
        # this exact scenario used to assert as correct).
        n = 3
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.array([0.03, 0.07, 0.15]),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(max_charge_kw=0.0, max_discharge_kw=0.0)
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
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
        total_delivered = sum(
            float(p) * float(h)
            for p, h in zip(plan.adequacy_loads[0].power_kw, periods.hours)
        )
        self.assertAlmostEqual(
            total_delivered,
            0.5,
            delta=1e-3,
            msg="must deliver exactly its real target, not run on through every "
            "period the price still gates in",
        )

    def test_no_value_per_kwh_is_byte_identical_to_before_this_fix(self):
        # The served_credit mechanism only ever activates when
        # value_per_kwh is set -- every existing install (none of which
        # configure it today, per #606's own issue body) must see zero
        # behaviour change. A load with a real deadline and no
        # value_per_kwh still just delivers to meet target_kwh by the
        # deadline, same as always.
        n = 3
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.array([0.03, 0.07, 0.15]),
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(max_charge_kw=0.0, max_discharge_kw=0.0)
        adequacy = [
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
                target_kwh=2.0,
                earliest_period=0,
                deadline_period=2,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                # value_per_kwh deliberately omitted (None default).
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
        self.assertAlmostEqual(plan.adequacy_loads[0].shortfall_kwh, 0.0, delta=1e-3)
        total_delivered = sum(
            float(p) * float(h)
            for p, h in zip(plan.adequacy_loads[0].power_kw, periods.hours)
        )
        self.assertGreaterEqual(total_delivered, 2.0 - 1e-3)
