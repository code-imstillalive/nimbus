"""Real, direct household ask (2026-08-31): "it should be smart to know
how to balance it with p2p in play as well as without it there at all...
there will be a variety of users... different plans different suppliers...
the integration must handle and allow for variables and various
scenarios." solver/stochastic.py (Track A2, the two-stage stochastic LP)
had zero P2P support at all -- this suite proves the extension
(solver/p2p_export.py, see that module's own docstring for the full
"why a shared module, not a network.py refactor" reasoning) is genuinely
wired in correctly, not just "doesn't crash":

  1. fixed_export_kw pins grid_export EXACTLY to the committed rate, in
     BOTH stage 1 (the shared, pre-branch decision) and EVERY stage-2
     scenario independently -- the whole point of a two-stage model is
     that stage 1 must work reasonably well regardless of which future
     materializes, so the commitment has to hold in every branch, not
     just the one a deterministic solve happened to assume.
  2. The hard charge gate holds under real economic pressure to violate
     it, in stage 1 AND every scenario -- mirrors network.py's own
     toughest fixed_export_kw test (test_solver_fixed_export.py).
  3. The two-tier export bonus's own per-real-calendar-day cap is
     genuinely INDEPENDENT per scenario, not accidentally shared across
     them -- each stochastic scenario represents a separate hypothetical
     future, not simultaneous execution, so each must be free to claim up
     to its own full cap regardless of what any other scenario claims.
     This is the one property genuinely specific to layering P2P onto a
     multi-scenario model rather than build_plan()'s own single-scenario
     case -- a shared/leaked cap here would silently starve every
     scenario after the first, the exact class of bug network.py's own
     2026-08-17 "per real calendar day, not once across the whole
     horizon" fix already had to catch once for the single-scenario case.
  4. None (every P2P field unset) is a complete, byte-identical no-op --
     the existing, pre-2026-08-31 test_solver_stochastic.py suite must
     keep passing unchanged, and this suite adds its own explicit check
     too, since the shared LP-construction helper this feature touches is
     used by every stochastic scenario, not just P2P ones.
  5. A broad scenario matrix (fixed_export on/off x export_bonus on/off)
     solves cleanly in all four combinations -- the direct, literal
     "handle and allow for variables and various scenarios" ask, not
     assumed from the individual mechanism tests alone.

Real, pre-existing gap also closed as part of this work (not new scope,
a genuine prerequisite): StochasticPlan never exposed grid_import_kw/
grid_export_kw at all before this -- fixed_export_kw pins grid_export
directly, so there would have been no way to even observe this mechanism
working from the result. See stochastic.py's own StochasticPlan docstring
comment for the full note.
"""

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.stochastic import build_stochastic_plan

# Matches test_solver_fixed_export.py's own real, live-derived values --
# deliberately reused, not re-invented, so this suite is directly
# comparable to the single-scenario case it extends.
_TARGET_KW = 11.5
_STOCHASTIC_START = 3  # periods 0,1,2 are stage 1
_N = 11  # stage 2 = periods 3..10 (8 periods): 3..9 is the P2P window, 10 is free


def _scenario(
    *,
    fixed_export: bool,
    bonus: bool,
    n_scenarios: int = 2,
    salvage_value: float = 0.15,
    window_import_price: float = 0.55,
):
    """periods 0-2: stage 1, a real pre-window charging opportunity
    (cheap import 0.15, matching test_solver_fixed_export.py's own
    scenario). periods 3-9: the real 7h P2P window. period 10: free,
    post-window. Both solar scenarios are flat zero throughout -- this
    suite deliberately isolates the P2P mechanism from solar-hedging
    behaviour, which test_solver_stochastic.py's own existing
    TestGenuineHedgingBehaviour suite already covers on its own.
    """
    hours = np.array([1.0] * _N)
    periods = PeriodGrid(hours=hours, start=datetime(2026, 8, 20, 14, 0, tzinfo=UTC))

    import_price = np.array([0.15, 0.15, 0.15] + [window_import_price] * 7 + [0.15])
    export_price = np.array([0.08, 0.08, 0.08] + [0.09] * 7 + [0.08])

    fixed_export_kw = None
    if fixed_export:
        fixed_export_kw = np.full(_N, np.nan)
        fixed_export_kw[3:10] = _TARGET_KW

    export_bonus_price = None
    export_bonus_volume_kwh = None
    if bonus:
        export_bonus_price = np.array([0.0, 0.0, 0.0] + [0.32] * 7 + [0.0])
        # Deliberately less than 7 * whatever the window would otherwise
        # deliver -- forces a real, non-trivial choice of which periods
        # claim it (same design as test_solver_export_bonus_tiebreak.py).
        export_bonus_volume_kwh = 40.0

    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        fixed_export_kw=fixed_export_kw,
        export_bonus_price=export_bonus_price,
        export_bonus_volume_kwh=export_bonus_volume_kwh,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=122.2 * 0.20,
        min_soc_kwh=122.2 * 0.05,
        max_soc_kwh=122.2 * 1.0,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=salvage_value,
    )
    solar_scenarios = [np.zeros(_N) for _ in range(n_scenarios)]
    weights = [0.6, 0.4] if n_scenarios == 2 else [1.0 / n_scenarios] * n_scenarios
    load_kw = np.full(_N, 1.5)
    return periods, grid, battery, solar_scenarios, weights, load_kw


class TestFixedExportAcrossStagesAndScenarios(unittest.TestCase):
    def test_fixed_export_pins_stage1_and_every_scenario(self):
        periods, grid, battery, solar_scenarios, weights, load_kw = _scenario(
            fixed_export=True, bonus=False
        )
        plan = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan.status, "optimal")

        # Stage 1 (periods 0-2) has no fixed commitment at all -- must NOT
        # coincidentally land on the same specific value as the window.
        self.assertFalse(
            all(abs(v - _TARGET_KW) < 0.01 for v in plan.stage1_grid_export_kw),
            "stage 1 export incorrectly pinned to the window's own rate",
        )

        # Every scenario's own window (relative indices 0..6 == absolute
        # periods 3..9) must be pinned EXACTLY, and the free trailing
        # period (relative index 7 == absolute period 10) must not be.
        for s in range(len(solar_scenarios)):
            window = plan.stage2_grid_export_kw[s][0:7]
            for i, v in enumerate(window):
                self.assertAlmostEqual(
                    v,
                    _TARGET_KW,
                    places=4,
                    msg=f"scenario {s}, period {3 + i}: expected exactly "
                    f"{_TARGET_KW}kW, got {v}",
                )
            post_window = plan.stage2_grid_export_kw[s][7]
            self.assertNotAlmostEqual(
                post_window,
                _TARGET_KW,
                places=1,
                msg=f"scenario {s}'s post-window period incorrectly pinned too",
            )

    def test_hard_charge_gate_holds_in_stage1_and_every_scenario_under_incentive(self):
        """Mirrors test_solver_fixed_export.py's own toughest test:
        cheap window import (0.05, well below the real ~0.55) plus a
        large salvage_value (2.0, well above every real price in this
        scenario) -- if the gate didn't exist, an unconstrained LP would
        clearly want to buy as much cheap window energy as possible and
        bank it for the terminal credit. The gate must hold anyway, in
        stage 1 (which has no fixed commitment of its own, but shares the
        same battery.max_charge_kw variable-construction path) and in
        every stage-2 scenario's own window.
        """
        periods, grid, battery, solar_scenarios, weights, load_kw = _scenario(
            fixed_export=True, bonus=False, salvage_value=2.0, window_import_price=0.05
        )
        plan = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan.status, "optimal")

        for s in range(len(solar_scenarios)):
            window_charge = plan.stage2_charge_kw[s][0:7]
            self.assertTrue(
                (window_charge <= 1e-6).all(),
                f"scenario {s}: charge nonzero during the fixed-export "
                f"window despite the hard gate: {window_charge}",
            )
            # The commitment itself must still hold exactly, completely
            # unaffected by the charging-incentive pressure.
            window_export = plan.stage2_grid_export_kw[s][0:7]
            np.testing.assert_allclose(window_export, _TARGET_KW, atol=1e-4)

        # And the gate must be scoped ONLY to the fixed window -- real
        # charging should still be genuinely possible in stage 1 (periods
        # 0-2, no commitment there) given the strong incentive to bank
        # cheap-ish energy for the terminal credit.
        self.assertGreater(
            float(np.sum(plan.stage1_charge_kw)),
            0.0,
            "expected real stage-1 charging under a strong terminal "
            "incentive, but the gate appears to have blocked it outside "
            "the fixed window too",
        )


class TestExportBonusIndependentPerScenario(unittest.TestCase):
    def test_bonus_cap_is_independent_per_scenario_not_shared(self):
        """The real, decisive check: each stochastic scenario represents
        a SEPARATE hypothetical future, not simultaneous execution -- the
        per-real-calendar-day export_bonus cap must apply independently
        within each scenario's own branch, never summed/shared across
        scenarios. If this were wrong (a leaked/shared cap), scenario 1
        would end up starved after scenario 0 "used up" its own share --
        exactly the class of bug network.py's own 2026-08-17 per-day fix
        already had to catch once for the simpler single-scenario case.
        """
        periods, grid, battery, solar_scenarios, weights, load_kw = _scenario(
            fixed_export=False, bonus=True
        )
        plan = build_stochastic_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan.status, "optimal")

        hours_window = periods.hours[3:10]
        for s in range(len(solar_scenarios)):
            claimed = float(np.sum(plan.stage2_export_bonus_kw[s][0:7] * hours_window))
            self.assertAlmostEqual(
                claimed,
                40.0,
                places=1,
                msg=f"scenario {s} claimed {claimed:.2f}kWh of its own "
                f"40kWh cap -- expected each scenario to independently "
                f"claim its FULL cap (plenty of real export capacity "
                f"exists to do so), not a shared/split total",
            )

        # And the tie-breaker still resolves to a single clean block per
        # scenario (LATEST-preferred, matching network.py's own fix) --
        # not a scattered, arbitrary ON/OFF pattern, same property
        # test_solver_export_bonus_tiebreak.py already proves for the
        # single-scenario case.
        for s in range(len(solar_scenarios)):
            on = [float(x) > 0.01 for x in plan.stage2_export_bonus_kw[s][0:7]]
            transitions = sum(1 for i in range(1, len(on)) if on[i] != on[i - 1])
            self.assertLessEqual(
                transitions,
                1,
                f"scenario {s}'s own export_bonus must form one clean "
                f"block, not flicker: {plan.stage2_export_bonus_kw[s][0:7]}",
            )


class TestNoOpWhenUnconfigured(unittest.TestCase):
    def test_none_p2p_fields_is_byte_identical_to_before_this_feature_existed(self):
        periods, grid_with_fields, battery, solar_scenarios, weights, load_kw = (
            _scenario(fixed_export=False, bonus=False)
        )
        # grid_with_fields already has fixed_export_kw/export_bonus_* set
        # to None via _scenario(fixed_export=False, bonus=False) -- build
        # a second GridConfig that never had those fields at all, and
        # confirm the two solves are indistinguishable.
        grid_without_fields = GridConfig(
            import_price=grid_with_fields.import_price,
            export_price=grid_with_fields.export_price,
            import_limit_kw=grid_with_fields.import_limit_kw,
            export_limit_kw=grid_with_fields.export_limit_kw,
        )
        plan_a = build_stochastic_plan(
            periods=periods,
            grid=grid_with_fields,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        plan_b = build_stochastic_plan(
            periods=periods,
            grid=grid_without_fields,
            battery=battery,
            solar_scenarios=solar_scenarios,
            scenario_weights=weights,
            stochastic_start_period=_STOCHASTIC_START,
            load_kw=load_kw,
        )
        self.assertEqual(plan_a.status, plan_b.status, "optimal")
        np.testing.assert_allclose(plan_a.stage1_charge_kw, plan_b.stage1_charge_kw)
        np.testing.assert_allclose(
            plan_a.stage1_grid_export_kw, plan_b.stage1_grid_export_kw
        )
        for s in range(len(solar_scenarios)):
            np.testing.assert_allclose(
                plan_a.stage2_grid_export_kw[s], plan_b.stage2_grid_export_kw[s]
            )
        self.assertAlmostEqual(
            plan_a.expected_total_cost, plan_b.expected_total_cost, places=6
        )
        # And every export_bonus array is genuinely all-zero, not merely
        # absent -- StochasticPlan's own "represent honestly" convention.
        self.assertEqual(float(np.sum(plan_a.stage1_export_bonus_kw)), 0.0)
        for s in range(len(solar_scenarios)):
            self.assertEqual(float(np.sum(plan_a.stage2_export_bonus_kw[s])), 0.0)


class TestBroadScenarioMatrix(unittest.TestCase):
    def test_all_four_fixed_export_x_bonus_combinations_solve_optimally(self):
        """Direct, literal proof of the household's own ask: "the
        integration must handle and allow for variables and various
        scenarios" -- every real combination a household could actually
        have (no P2P plan at all; a fixed-rate-only plan; a bonus-only
        plan; both together) must solve cleanly, not just the one
        combination the other tests in this file happen to focus on.
        """
        for fixed_export in (False, True):
            for bonus in (False, True):
                with self.subTest(fixed_export=fixed_export, bonus=bonus):
                    periods, grid, battery, solar_scenarios, weights, load_kw = (
                        _scenario(fixed_export=fixed_export, bonus=bonus)
                    )
                    plan = build_stochastic_plan(
                        periods=periods,
                        grid=grid,
                        battery=battery,
                        solar_scenarios=solar_scenarios,
                        scenario_weights=weights,
                        stochastic_start_period=_STOCHASTIC_START,
                        load_kw=load_kw,
                    )
                    self.assertEqual(
                        plan.status,
                        "optimal",
                        f"fixed_export={fixed_export}, bonus={bonus} failed to solve",
                    )


if __name__ == "__main__":
    unittest.main()
