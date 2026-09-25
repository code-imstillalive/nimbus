"""Real test of solver/backtest.py -- the retrospective parameter-
sensitivity engine (2026-08-25, direct household ask for a genuine
"outstanding, unique" idea: an offline backtesting engine that proves
Nimbus's own decisions against real history, rather than a bigger LP
or a fancier model).

Imports and exercises the REAL functions (score_candidate_day,
run_efficiency_sensitivity_sweep) against the REAL oracle_dispatch()/
evaluate_realized_cost()/build_plan() machinery already proven by
regret.py/quality_report.py's own real usage -- not a reimplementation
or a mock of the LP.
"""

import itertools
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.backtest import (
    EFFICIENCY_CANDIDATES_PERCENT,
    CandidateResult,
    efficiency_label,
    run_efficiency_sensitivity_sweep,
    score_candidate_day,
)
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 30.0,
        "initial_soc_kwh": 25.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 30.0,
        "max_charge_kw": 15.0,
        "max_discharge_kw": 15.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.10,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _real_day_scenario(n: int = 24):
    """A real-shaped 24h day: cheap overnight import, expensive evening
    peak, solar midday, a genuine arbitrage opportunity -- deliberately
    NOT flat/degenerate, so a lower efficiency candidate can genuinely
    produce a real, different, worse score (a flat-price day would make
    every efficiency candidate score identically, proving nothing)."""
    periods = _flat_grid(n)
    hour = np.arange(n)
    import_price = np.where((hour >= 17) & (hour < 21), 0.45, 0.20)
    export_price = np.full(n, 0.05)
    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    solar_kw = np.where((hour >= 9) & (hour < 15), 6.0, 0.0)
    solar = SolarConfig(forecast_kw=solar_kw)
    load_kw = np.full(n, 1.0)
    load = LoadConfig(name="load", forecast_kw=load_kw)
    return periods, grid, solar, load


class TestScoreCandidateDay(unittest.TestCase):
    def test_returns_a_real_finite_cost(self):
        periods, grid, solar, load = _real_day_scenario()
        cost = score_candidate_day(
            periods=periods, grid=grid, battery=_base_battery(), solar=solar, load=load
        )
        self.assertTrue(np.isfinite(cost))

    def test_worse_efficiency_never_produces_a_better_score_than_near_perfect_efficiency(
        self,
    ):
        # A real, physically-grounded invariant: near-perfect round-trip
        # efficiency gives the oracle strictly more (or equal) freedom to
        # arbitrage than any lossier candidate -- so 99% must score at
        # least as well (lower or equal total_cost) as a lossier one on
        # the SAME real day. Not exactly 100% -- BatteryConfig itself
        # rejects that as a real degeneracy guard (see backtest.py's own
        # EFFICIENCY_CANDIDATES_PERCENT comment).
        periods, grid, solar, load = _real_day_scenario()
        perfect = score_candidate_day(
            periods=periods,
            grid=grid,
            battery=_base_battery(charge_efficiency=0.99, discharge_efficiency=0.99),
            solar=solar,
            load=load,
        )
        lossy = score_candidate_day(
            periods=periods,
            grid=grid,
            battery=_base_battery(charge_efficiency=0.70, discharge_efficiency=0.70),
            solar=solar,
            load=load,
        )
        self.assertLessEqual(perfect, lossy + 1e-6)


class TestRunEfficiencySensitivitySweep(unittest.TestCase):
    def test_returns_one_result_per_candidate_on_a_real_solvable_day(self):
        periods, grid, solar, load = _real_day_scenario()
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=grid,
            base_battery=_base_battery(),
            solar=solar,
            load=load,
        )
        self.assertEqual(len(results), len(EFFICIENCY_CANDIDATES_PERCENT))
        for r in results:
            self.assertIsInstance(r, CandidateResult)
            self.assertTrue(np.isfinite(r.total_cost))

    def test_results_are_labeled(self):
        periods, grid, solar, load = _real_day_scenario()
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=grid,
            base_battery=_base_battery(),
            solar=solar,
            load=load,
        )
        by_label = {r.label: r.total_cost for r in results}
        self.assertEqual(set(by_label), {"85%", "90%", "95%", "99%"})

    def test_the_sweep_is_not_monotonic_in_the_candidate(self):
        """nimbus issue #1232 -- this replaces a test that asserted the
        OPPOSITE, and the replaced test is worth remembering.

        It was called `test_results_are_labeled_and_monotonic_with_
        efficiency`, and its comment called monotonicity "the real,
        interpretable 'does efficiency actually matter here' signal this
        whole feature exists to produce." The belief was that a higher
        assumed efficiency must never score worse.

        It cannot score worse *while every candidate is scored under its
        own assumption*, which is what the sweep used to do: a battery
        assumed 99% efficient plans more cycling, is then charged only 1%
        loss per leg, and reaches a higher final SoC credited at
        `salvage_value`. Monotonic by construction -- so `best_candidate`
        was always simply the largest candidate and carried no
        information. Measured live on the reference household 2026-09-24:
        85% -> -33.32, 90% -> -34.99, 95% -> -36.53, 99% -> -37.74, with
        the sensor publishing `best_candidate: 99%` against a configured
        85.8%. That reads as advice toward a setting `BatteryConfig`
        rejects at 100% and the Solver-settings wizard explicitly warns
        against.

        Scoring every candidate under the CONFIGURED battery removes the
        guarantee, which is the entire point: over-assuming efficiency now
        costs real money in the score.
        """
        periods, grid, solar, load = _real_day_scenario()
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=grid,
            base_battery=_base_battery(),
            solar=solar,
            load=load,
        )
        by_label = {r.label: r.total_cost for r in results}
        ordered = [
            by_label[efficiency_label(p)] for p in sorted(EFFICIENCY_CANDIDATES_PERCENT)
        ]
        self.assertFalse(
            all(b <= w + 1e-6 for w, b in itertools.pairwise(ordered)),
            "the sweep is still monotonic in the candidate, so "
            "best_candidate still carries no information: " + repr(ordered),
        )

    def test_the_sweep_recovers_the_batterys_own_real_efficiency(self):
        """The strongest available check that the sweep measures
        something at all (nimbus issue #1232).

        The scenario battery's real round-trip is `charge_efficiency *
        discharge_efficiency`. Sweeping that value alongside the fixed
        candidates, the best-scoring candidate must be the one matching
        the physics the day is scored under -- not the largest. A sweep
        that identifies the efficiency it is actually running on is
        measuring; one that always answers 99% is not.
        """
        periods, grid, solar, load = _real_day_scenario()
        base = _base_battery()
        true_rt = round(base.charge_efficiency * base.discharge_efficiency * 100, 2)
        swept = tuple(sorted({*EFFICIENCY_CANDIDATES_PERCENT, true_rt}))
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=grid,
            base_battery=base,
            solar=solar,
            load=load,
            candidates_percent=swept,
        )
        best = min(results, key=lambda r: r.total_cost)
        self.assertEqual(
            best.label,
            efficiency_label(true_rt),
            "the best candidate should be the battery's own real "
            f"efficiency, got {best.label}; costs="
            + repr({r.label: round(r.total_cost, 4) for r in results}),
        )

    def test_the_label_keeps_a_fractional_configured_value_distinct(self):
        """A real install's configured value is swept too (#1232), so 85.8
        must not render as '86%' -- indistinguishable from a genuine 86%
        candidate."""
        self.assertEqual(efficiency_label(85.8), "85.8%")
        self.assertEqual(efficiency_label(90.0), "90%")

    def test_a_genuinely_infeasible_candidate_is_skipped_not_fatal(self):
        # Real defensive guarantee: one candidate's own infeasibility
        # must not abort the whole sweep. An absurdly tight max_charge/
        # discharge_kw (0.0 -- the battery physically cannot move at
        # all) combined with a real load the grid import limit can't
        # cover on its own is a genuine, real infeasibility.
        periods, grid, solar, load = _real_day_scenario()
        tiny_grid = GridConfig(
            import_price=grid.import_price,
            export_price=grid.export_price,
            import_limit_kw=0.0001,
            export_limit_kw=0.0001,
        )
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=tiny_grid,
            base_battery=_base_battery(max_charge_kw=0.0, max_discharge_kw=0.0),
            solar=solar,
            load=load,
        )
        # Must not raise -- whatever it returns (possibly empty) is fine.
        self.assertIsInstance(results, list)

    def test_custom_candidate_list_is_respected(self):
        periods, grid, solar, load = _real_day_scenario()
        results = run_efficiency_sensitivity_sweep(
            periods=periods,
            grid=grid,
            base_battery=_base_battery(),
            solar=solar,
            load=load,
            candidates_percent=(80.0, 99.0),
        )
        self.assertEqual({r.label for r in results}, {"80%", "99%"})
