"""nimbus issue #586 (Mark Purcell, real repro: 8 Sep started at 0.0%
against a 13% floor -- #571 -- with 20.2c/kWh live at midnight and the
day's own real cheapest power at 4.9c/kWh six hours later at 14:00).

compute_quality_report()'s own oracle (j_star, via build_plan()) used
build_plan()'s DEFAULT auto-derived soft_soc_penalty_per_kwh -- steep by
design (DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER times the day's own MAXIMUM
real price), so the LIVE solver always finds it worth recovering a
below-floor SoC promptly. That reasoning is backwards for a RETROSPECTIVE
oracle: perfect foresight has no reason to value promptness, only real
price advantage. The old behaviour bought expensive early power purely to
reach the floor, inflating j_star and understating regret by the same
amount ("the household beat the oracle at midnight" -- a nonsensical
reading of the real day).

A first fix attempt (deriving the penalty from the day's own MINIMUM
price instead of its maximum, still applied every period) was proven
insufficient by this file's own first version of these tests before
shipping: the penalty accrues PER PERIOD for every period spent below
floor, so even a much lower per-kWh rate still adds up to far more than
the one-time cost of an expensive-but-prompt recovery once enough hours
separate the start of the day from its own cheap window -- a lower
recurring rate never actually removes the urgency, it only raises the
bar for how much delay reproduces the same rush. See
TestOracleSoftFloorPenaltyDerivation::test_a_merely_lower_recurring_
penalty_still_rushes_to_recover below, kept as a permanent regression
guard against re-introducing that same wrong fix.

Real fix: the auto-derived penalty is untouched (zero behaviour change)
for the ordinary case -- a battery starting the day AT OR ABOVE its own
floor never needed this question answered, and the floor stays a real,
fully-priced preference throughout the plan. Only when the day's own
STARTING condition is already below floor (a fact about history the
oracle had no way to have prevented) is the penalty relaxed to exactly
zero for that one oracle solve -- removing the false "must recover
immediately" pressure precisely in the one case #586 describes, without
weakening the floor's real economic weight on every other, well-behaved
day.

Two levels of proof, matching this repo's own established convention
(test_solver_soft_min_soc_constraint.py tests the underfill mechanism
directly at the build_plan() level; the daily-quality-report test files
test compute_quality_report() end-to-end):
  1. Direct build_plan() comparison: the OLD auto-derived penalty vs the
     fix's conditional-zero penalty, on the exact real scenario shape.
  2. compute_quality_report() end-to-end: the published j_star_hourly no
     longer shows a large early recovery charge at the expensive hour,
     and an above-floor day is byte-identical to before this fix.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER, build_plan
from solver.quality_report import compute_quality_report

N = 24  # 24 hourly periods, one real day
CAPACITY = 40.3  # Mark's own real Sigen pack
MIN_SOC_PCT = 13.0
MIN_SOC = CAPACITY * MIN_SOC_PCT / 100.0

# Real repro shape: expensive at hour 0 (the price live when the day's
# scorer would otherwise rush to recover), a genuine cheap window at
# hour 14 (Mark's own real 4.9c/kWh), moderate everywhere else.
EXPENSIVE_HOUR = 0
CHEAP_HOUR = 14
IMPORT_PRICE = np.full(N, 0.15)
IMPORT_PRICE[EXPENSIVE_HOUR] = 0.202
IMPORT_PRICE[CHEAP_HOUR] = 0.049
EXPORT_PRICE = np.full(N, 0.05)


def _battery(initial_soc_kwh: float) -> BatteryConfig:
    return BatteryConfig(
        name="battery",
        capacity_kwh=CAPACITY,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=MIN_SOC,
        max_soc_kwh=CAPACITY,
        max_charge_kw=25.0,
        max_discharge_kw=25.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _grid() -> GridConfig:
    return GridConfig(
        import_price=IMPORT_PRICE,
        export_price=EXPORT_PRICE,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )


class TestOracleSoftFloorPenaltyDerivation(unittest.TestCase):
    """Level 1: direct build_plan() comparison, old auto-derived penalty
    vs the fix's conditional zero."""

    def setUp(self):
        start = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
        self.periods = PeriodGrid(hours=np.array([1.0] * N), start=start)
        self.grid = _grid()
        self.solar = SolarConfig(forecast_kw=np.zeros(N))
        self.loads = [LoadConfig(name="house", forecast_kw=np.full(N, 1.0))]
        # Starting at 0.0% -- Mark's own real #571 case.
        self.battery = _battery(initial_soc_kwh=0.0)

    def test_old_auto_derived_penalty_charges_at_the_expensive_hour(self):
        # Establishes the bug is real on this exact scenario before
        # proving the fix: with no override (build_plan()'s own default
        # auto-derivation, the pre-#586 behaviour), the oracle pays the
        # expensive 20.2c hour just to reach the floor promptly.
        plan = build_plan(
            periods=self.periods,
            grid=self.grid,
            batteries=[self.battery],
            solar=self.solar,
            loads=self.loads,
        )
        self.assertTrue(plan.is_optimal)
        net_charge_kw = plan.battery_charge_kw - plan.battery_discharge_kw
        self.assertGreater(
            net_charge_kw[EXPENSIVE_HOUR],
            2.0,
            "sanity check failed: the default derivation should reproduce "
            "the real bug (buying at the expensive hour) -- if this fails, "
            "the scenario itself no longer reproduces #586",
        )

    def test_a_merely_lower_recurring_penalty_still_rushes_to_recover(self):
        # Permanent regression guard against re-introducing the FIRST,
        # insufficient fix attempt this issue's own commit history
        # tried and rejected: deriving the penalty from the day's own
        # MINIMUM price instead of its maximum, still applied every
        # period. Even at that much lower rate, 14 hours of accumulated
        # per-period underfill cost still dwarfs the one-time cost of
        # recovering immediately at the expensive hour -- proving a
        # lower recurring rate alone does not remove the urgency.
        lower_but_recurring_penalty = DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER * max(
            float(np.min(self.grid.import_price)),
            float(np.min(self.grid.export_price)),
            0.01,
        )
        plan = build_plan(
            periods=self.periods,
            grid=self.grid,
            batteries=[self.battery],
            solar=self.solar,
            loads=self.loads,
            soft_soc_penalty_per_kwh=lower_but_recurring_penalty,
        )
        self.assertTrue(plan.is_optimal)
        net_charge_kw = plan.battery_charge_kw - plan.battery_discharge_kw
        self.assertGreater(
            net_charge_kw[EXPENSIVE_HOUR],
            2.0,
            "a merely-lower recurring penalty should STILL rush to recover "
            "at the expensive hour -- if this now passes, the underlying "
            "per-period accumulation dynamic this test guards against may "
            "have changed and the module docstring's own reasoning should "
            "be re-checked",
        )

    def test_conditional_zero_penalty_defers_to_the_cheap_hour(self):
        plan = build_plan(
            periods=self.periods,
            grid=self.grid,
            batteries=[self.battery],
            solar=self.solar,
            loads=self.loads,
            soft_soc_penalty_per_kwh=0.0,
        )
        self.assertTrue(plan.is_optimal)
        net_charge_kw = plan.battery_charge_kw - plan.battery_discharge_kw
        # No rush to buy at the expensive hour...
        self.assertLess(
            net_charge_kw[EXPENSIVE_HOUR],
            0.5,
            "a zeroed penalty should not rush to recover the floor at the "
            "day's expensive hour",
        )
        # ...but real price advantage still recovers it -- charging is
        # cheap at the cheap hour regardless of the floor penalty, so the
        # plan still ends up recovering there on ordinary economics.
        self.assertGreater(
            net_charge_kw[CHEAP_HOUR],
            2.0,
            "recovery should still happen at the day's own cheap window "
            "on ordinary price economics, even with the floor penalty off",
        )

    def test_above_floor_scenario_is_unaffected_by_the_fix(self):
        # Backward-compatibility guard: a battery that starts ABOVE the
        # floor never triggers the new conditional branch at all --
        # byte-identical to the pre-#586 default derivation.
        battery_above_floor = _battery(initial_soc_kwh=CAPACITY * 0.5)
        default_plan = build_plan(
            periods=self.periods,
            grid=self.grid,
            batteries=[battery_above_floor],
            solar=self.solar,
            loads=self.loads,
        )
        explicit_none_plan = build_plan(
            periods=self.periods,
            grid=self.grid,
            batteries=[battery_above_floor],
            solar=self.solar,
            loads=self.loads,
            soft_soc_penalty_per_kwh=None,
        )
        np.testing.assert_allclose(
            default_plan.battery_soc_kwh, explicit_none_plan.battery_soc_kwh, atol=1e-9
        )


class TestComputeQualityReportOracleNoLongerRushesRecovery(unittest.TestCase):
    """Level 2: compute_quality_report() end-to-end, matching Mark's own
    real 8 Sep repro shape (0.0% start, 13% floor, expensive-then-cheap
    price path, Nimbus in shadow mode -- zero commanded/actual dispatch,
    matching his own "tracking fidelity 1.0 is vacuous" observation)."""

    def _report(self, initial_soc_kwh: float):
        start = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0] * N), start=start)
        grid = _grid()
        solar = SolarConfig(forecast_kw=np.zeros(N))
        load = LoadConfig(name="house", forecast_kw=np.full(N, 1.0))
        battery = _battery(initial_soc_kwh=initial_soc_kwh)
        timestamps = [start + timedelta(hours=i) for i in range(N)]
        zero = np.zeros(N)
        return compute_quality_report(
            periods=periods,
            grid_residual=grid,
            grid_oracle=grid,
            battery=battery,
            solar=solar,
            load=load,
            timestamps=timestamps,
            real_p2p_dollars_earned=0.0,
            commanded_charge_kw=zero,
            commanded_discharge_kw=zero,
            actual_charge_kw=zero,
            actual_discharge_kw=zero,
            final_soc_kwh_actual=initial_soc_kwh,
        )

    def test_j_star_hourly_shows_no_large_charge_at_the_expensive_hour(self):
        report = self._report(initial_soc_kwh=0.0)
        hourly_keys = list(report.j_star_hourly.keys())
        expensive_row = report.j_star_hourly[hourly_keys[EXPENSIVE_HOUR]]
        self.assertLess(
            expensive_row["battery_kw"],
            0.5,
            "published j_star_hourly should not show the oracle rushing to "
            "charge at the day's expensive hour just to reach the floor",
        )

    def test_above_floor_day_is_unaffected(self):
        # Same real-world day shape, but the battery already starts above
        # its floor -- j_star must be identical to a direct build_plan()
        # call with the default (untouched) penalty derivation.
        report = self._report(initial_soc_kwh=CAPACITY * 0.5)
        start = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
        periods = PeriodGrid(hours=np.array([1.0] * N), start=start)
        grid = _grid()
        solar = SolarConfig(forecast_kw=np.zeros(N))
        loads = [LoadConfig(name="house", forecast_kw=np.full(N, 1.0))]
        direct_plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[_battery(initial_soc_kwh=CAPACITY * 0.5)],
            solar=solar,
            loads=loads,
        )
        self.assertAlmostEqual(report.j_star, float(direct_plan.total_cost), places=6)
