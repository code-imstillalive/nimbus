"""Mark Purcell's own 9-item Solver audit, item #4: contract risk /
match failure. The real household risk this addresses: the Solver's
own export_bonus_volume_kwh is an ASSUMPTION about how much real P2P
volume LocalVolts will actually match tonight (see this project's own
p2p_recent_avg_volume_kwh() -- a real empirical average of RECENT
settled nights, not a guarantee). Real question: if the actual matched
volume comes in LOWER than assumed, does the Solver (a) remain
numerically stable (never crash/go infeasible over a wrong assumption),
and (b) does the SCORING machinery (quality_report.py) correctly show
the real shortfall as worse economic outcome, not silently absorb it?

A contract-risk audit that only checked "does it crash" would miss the
real point -- a Solver that stays numerically stable but whose own
scoring is BLIND to a real revenue shortfall would look fine while
quietly hiding real risk from the household. Both properties are
tested here, separately.
"""
import unittest
from datetime import datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from solver.network import build_plan
from solver.quality_report import compute_quality_report


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = dict(
        capacity_kwh=30.0, initial_soc_kwh=25.0, min_soc_kwh=2.0, max_soc_kwh=30.0,
        max_charge_kw=15.0, max_discharge_kw=15.0, charge_efficiency=0.99, discharge_efficiency=0.99,
        charge_cost=0.01, discharge_cost=0.01, salvage_value=0.10,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


class TestSolverStabilityUnderWrongContractAssumption(unittest.TestCase):
    """The Solver itself must never crash or go infeasible just because
    a caller's own export_bonus_volume_kwh assumption turns out to be
    wrong at solve time -- the volume cap is a real UPPER bound on how
    much can be CLAIMED, never a promise that volume physically must be
    delivered, so an unrealistically high assumption should just go
    unclaimed past whatever's physically achievable, not break anything.
    """

    def test_wildly_optimistic_volume_cap_does_not_break_the_solve(self):
        n = 6
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.50), export_price=np.full(n, 0.08),
            import_limit_kw=10.0, export_limit_kw=10.0,
            export_bonus_price=np.full(n, 0.40),
            # Deliberately absurd: far more than this battery could ever
            # physically deliver in 6 hours (max 15kW x 6h = 90kWh even
            # at full discharge every period, and initial_soc is only
            # 25kWh) -- a real, over-optimistic contract assumption.
            export_bonus_volume_kwh=10_000.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        loads = [LoadConfig(name="load", forecast_kw=np.zeros(n))]
        plan = build_plan(periods=periods, grid=grid, battery=_base_battery(), solar=solar, loads=loads)
        self.assertEqual(plan.status, "optimal", "an over-optimistic volume cap must never make the LP infeasible or crash")
        total_claimed = float(np.sum(plan.export_bonus_kw))
        # Physically bounded by real battery energy, not by the absurd cap.
        self.assertLess(total_claimed, 30.0, "claimed bonus volume must stay within what's physically deliverable, regardless of an unrealistic cap")


class TestScoringDetectsRealMatchShortfall(unittest.TestCase):
    """The real point of this audit item: if LESS P2P volume actually
    gets matched tonight than the Solver's own plan assumed, does the
    quality_report.py scoring correctly reflect that as a real,
    measurable economic hit -- or does it silently ignore the gap?
    """

    def _plan_and_report(self, real_p2p_dollars_earned: float):
        n = 4
        periods = _flat_grid(n)
        grid_residual = GridConfig(
            import_price=np.full(n, 0.50), export_price=np.full(n, 0.06),
            import_limit_kw=10.0, export_limit_kw=10.0,
        )
        grid_oracle = GridConfig(
            import_price=np.full(n, 0.50), export_price=np.full(n, 0.06),
            import_limit_kw=10.0, export_limit_kw=10.0,
            export_bonus_price=np.full(n, 0.40), export_bonus_volume_kwh=15.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        load = LoadConfig(name="load", forecast_kw=np.full(n, 1.0))
        battery = _base_battery(initial_soc_kwh=20.0)
        # Real bug caught here first run: hourly_regret_breakdown() reads
        # .hour off each timestamp directly (it bins BY hour-of-day) --
        # a None placeholder crashes, real datetimes are required.
        timestamps = [datetime(2026, 8, 17, h) for h in range(n)]

        # Real actual dispatch: matches what a well-behaved automation
        # would do to chase the full assumed 15kWh bonus (14kW steady
        # discharge for 1h serves load + exports the rest).
        actual_discharge = np.full(n, 14.0)
        actual_charge = np.zeros(n)

        report = compute_quality_report(
            periods=periods, grid_residual=grid_residual, grid_oracle=grid_oracle,
            battery=battery, solar=solar, load=load, timestamps=timestamps,
            real_p2p_dollars_earned=real_p2p_dollars_earned,
            commanded_charge_kw=actual_charge, commanded_discharge_kw=actual_discharge,
            actual_charge_kw=actual_charge, actual_discharge_kw=actual_discharge,
            final_soc_kwh_actual=20.0 - float(np.sum(actual_discharge)) / battery.discharge_efficiency,
        )
        return report

    def test_lower_real_match_produces_worse_score_than_full_match(self):
        # Full assumed match: ~15kWh at the real 0.40 bonus premium (on
        # top of the 0.06 base rate already earned via export_price) --
        # a real, self-consistent "everything matched as planned" figure.
        full_match_dollars = 15.0 * 0.40
        report_full_match = self._plan_and_report(real_p2p_dollars_earned=full_match_dollars)

        # Real shortfall: LocalVolts only actually matched half of it.
        half_match_dollars = full_match_dollars * 0.5
        report_half_match = self._plan_and_report(real_p2p_dollars_earned=half_match_dollars)

        self.assertLess(
            report_full_match.j_ach, report_half_match.j_ach,
            "a real, lower P2P match should make J_ach (realized cost) genuinely WORSE (higher/less negative) -- "
            "if it doesn't, the scoring is blind to real match-failure risk",
        )
        self.assertGreater(
            report_half_match.epr.epr, -1e9,  # sane, finite, not NaN/inf
        )
        self.assertLess(
            report_half_match.epr.epr, report_full_match.epr.epr,
            "EPR should genuinely drop when real match volume comes in lower than assumed -- "
            "this is the real, direct answer to 'does the score reflect contract risk'",
        )
        self.assertFalse(np.isnan(report_half_match.epr.epr), "EPR must stay a real, finite number even under a real shortfall, never NaN")

    def test_zero_real_match_does_not_crash_the_scoring(self):
        """The real worst case: LocalVolts matches NOTHING tonight
        despite the plan assuming a real bonus was available. The
        scoring machinery must still produce a real, finite (if bad)
        number -- not crash, not silently return a fake-looking good
        score."""
        report_zero_match = self._plan_and_report(real_p2p_dollars_earned=0.0)
        self.assertFalse(np.isnan(report_zero_match.epr.epr))
        self.assertFalse(np.isinf(report_zero_match.j_ach))
        full_match_dollars = 15.0 * 0.40
        report_full_match = self._plan_and_report(real_p2p_dollars_earned=full_match_dollars)
        self.assertLess(
            report_zero_match.epr.epr, report_full_match.epr.epr,
            "a total real match failure should score strictly worse than a full real match",
        )


if __name__ == "__main__":
    unittest.main()
