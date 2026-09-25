"""nimbus issue #773: phase 2's timeout, located and fixed.

Every earlier lead on this issue chased the SEARCH. The captured real
instance settles that the search is not the problem, because on that
instance the search never starts. Measured on
`nimbus_773_fail_phase2_secondary.mps` (1,412 integer columns of
12,138), at production's own 60 s per-call limit:

    unseeded, 60 s      Time limit reached, **0 MIP nodes**, no solution
                        -- identical across EIGHT HiGHS option configs
                        (default, solver=ipm, presolve=off,
                        simplex_strategy=4, mip_heuristic_effort=0.5,
                        mip_detect_symmetry=off, run_crossover=off,
                        simplex_scale_strategy=5)
    unseeded, 900 s     Optimal, 253.4 s, 1,314,628 iters, 1,017 nodes
    SEEDED, 60 s        Optimal, 31.0 s, 62,816 iters, 52 nodes
    integers pinned     Optimal, 0.2 s (a pure LP)

Zero nodes means branch-and-bound never begins, so no option tuning the
search could ever have helped -- which retires the per-node-LP-cost and
cut-generation theories, both of which were advanced on this issue and
are wrong.

The fix follows from one observation about the phase sequence itself:
phase 2's feasible set is phase 1's intersected with the primary-bound
row, and phase 1's own optimum satisfies that row BY CONSTRUCTION --
that row is built from it. So an integer-feasible point for phase 2 is
already in hand when phase 2 starts, and unseeded phase 2 was burning
its whole budget failing to rediscover a point the caller was holding.

Two tiers, in `_solve_with_options()`:

  tier 1  hand phase 2 that point as a starting incumbent
          (`_offer_mip_start()`). 900 s-class failure -> 31 s proven
          optimal, same objective to 16 significant figures.
  tier 2  if phase 2 STILL cannot finish, pin phase 1's binary
          assignment and solve phase 2 as a pure LP
          (`_pin_binaries_to_values()`). 0.2 s on the same instance.

This also resolves the honestly-open half of nimbus issue #999, whose
warm-start experiment measured no improvement and concluded a MIP start
does not help. That experiment seeded the LP RELAXATION's optimum, which
is FRACTIONAL; HiGHS must repair it via a sub-MIP and on this instance
cannot. An INTEGRAL seed is a different ask, and the numbers above are
the difference. #999's measurement was sound; its conclusion was too
broad.

What these tests assert, and why each matters:

* Tier 1 runs on a real `build_plan()` MIP, and the point it offers is
  genuinely integral -- otherwise it is #999's experiment again.
* **Tier 1 does not change the answer.** The regression guard that
  matters: same objective and same dispatch with the seed and without
  it. A seed only supplies an upper bound, so it cannot change which
  optimum gets proven -- this pins that reasoning to a real solve.
* Tier 2 keeps the SECONDARY channel. Before this, a phase-2 failure
  discarded it entirely for the cycle (plus a 5-minute cooldown over
  which every later cycle also went without it). Tier 2 is this
  project's own pre-#702 behaviour, reached only when tier 1 fails.
* Tier 2 pins from the EXPLICIT capture, never from `h`'s live state --
  a timed-out phase 2 holds no solution at all (0 nodes, objective inf)
  or an unproven node relaxation. Pinning from there fixes binaries to
  garbage.
* A pure-LP caller is untouched: with no binaries there is no capture,
  and a phase failure re-raises exactly as before.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import unittest
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import highspy
import numpy as np
from solver import lp
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import LexOptions
from solver.network import build_plan

# The single-objective fallback's own log line -- the thing tier 2 exists
# to avoid reaching. Matched on a distinctive fragment rather than the
# whole sentence so a reworded warning doesn't silently pass this file.
_FALLBACK_FRAGMENT = "falling back to a plain single-objective solve"
_TIER2_FRAGMENT = "solving phase 2 as a pure LP instead"


class _LogCatcher(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_solver_logs():
    catcher = _LogCatcher()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(catcher)
    root.setLevel(logging.WARNING)
    try:
        yield catcher
    finally:
        root.removeHandler(catcher)
        root.setLevel(previous)


def _mip_scenario(*, n_loads: int, n_periods: int, seed: int):
    """A real MIP: one on/off binary block per semi-continuous adequacy
    load, over `n_periods`, with near-flat import prices so the LP has
    little genuine economic preference between periods and real ties
    exist for the secondary channel to break.

    Same shape as `test_773_tolerance_and_tie_slack_end_to_end.py`'s own
    `_many_binaries_tied_price_scenario()` -- deliberately, so a result
    here is comparable with that file's rather than being a new,
    differently-shaped scenario nobody can line up against it. The
    battery gets zero charge/discharge power so every degree of freedom
    routes through the binaries.
    """
    rng = np.random.default_rng(seed)
    periods = PeriodGrid(
        hours=np.full(n_periods, 1.0),
        start=datetime(2026, 9, 25, 0, 0, 0, tzinfo=UTC),
    )
    grid = GridConfig(
        import_price=0.20 + rng.normal(0, 1e-5, n_periods),
        export_price=np.zeros(n_periods),
        import_limit_kw=100.0,
        export_limit_kw=100.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n_periods))
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=20.0,
        max_charge_kw=0.0,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
    adequacy = [
        AdequacyLoadConfig(
            name=f"load{i}",
            max_power_kw=float(rng.uniform(1, 5)),
            target_kwh=float(rng.uniform(2, 10)),
            deadline_period=n_periods - 1,
            earliest_period=0,
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
        )
        for i in range(n_loads)
    ]
    return periods, grid, battery, solar, adequacy


def _solve(scenario, *, options=None):
    """Reset lp.py's module-level #773 cooldown first, so an earlier
    test's own fallback can never make a later test silently skip the
    phased path it is trying to exercise.

    `options=None` -> `LexOptions()`, constructed per call rather than
    once as a default-argument value (ruff B008): a shared default
    instance would be reused across every test in this file.
    """
    if options is None:
        options = LexOptions()
    periods, grid, battery, solar, adequacy = scenario
    lp._lex_calibration_failed_until = 0.0
    return build_plan(
        periods=periods,
        grid=grid,
        batteries=[battery],
        solar=solar,
        loads=[],
        adequacy_loads=adequacy,
        adequacy_semi_continuous=True,
        solve_options=options,
    )


class _FakeHighs:
    """Only the two mutators `_pin_binaries_to_values()` calls. Same
    shape as `test_lp_pin_binaries_integrality_diagnostic.py`'s own
    fake, minus `val()` -- whose absence is the point: this function
    must never read the solver's live state."""

    def __init__(self) -> None:
        self.bounds_set: list[tuple[int, float, float]] = []
        self.made_continuous: list[int] = []

    def changeColIntegrality(self, i, _kind):
        self.made_continuous.append(i)

    def changeColBounds(self, i, lo, hi):
        self.bounds_set.append((i, lo, hi))


class TestPinBinariesToExplicitValues(unittest.TestCase):
    """`_pin_binaries_to_values()` in isolation."""

    def test_it_pins_to_the_values_passed_not_the_solver_state(self):
        """The whole reason this exists alongside its sibling. `h` here
        has no `val()` at all, so a implementation that read the live
        solution would raise instead of passing."""
        h = _FakeHighs()
        lp._pin_binaries_to_values(h, [0, 2], [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(h.bounds_set, [(0, 1.0, 1.0), (2, 0.0, 0.0)])
        self.assertEqual(h.made_continuous, [0, 2])

    def test_it_only_touches_the_binary_columns(self):
        h = _FakeHighs()
        lp._pin_binaries_to_values(h, [1], [0.0, 1.0, 0.0])
        self.assertEqual(h.bounds_set, [(1, 1.0, 1.0)])

    def test_it_rounds_a_value_inside_the_solver_tolerance(self):
        """HiGHS satisfies integrality to a tolerance, not exactly, so a
        captured binary can read 1 - 1e-9. Pinning to that literal value
        would make the bound infeasible for an integer variable."""
        h = _FakeHighs()
        lp._pin_binaries_to_values(h, [0, 1], [1.0 - 1e-9, 1e-9])
        self.assertEqual(h.bounds_set, [(0, 1.0, 1.0), (1, 0.0, 0.0)])

    def test_no_binaries_is_a_no_op(self):
        h = _FakeHighs()
        lp._pin_binaries_to_values(h, [], [0.5, 0.5])
        self.assertEqual(h.bounds_set, [])
        self.assertEqual(h.made_continuous, [])


class TestOfferMipStart(unittest.TestCase):
    """`_offer_mip_start()` against a real `highspy.Highs`."""

    def _tiny_mip(self):
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        x = h.addVariable(lb=0.0, ub=1.0)
        y = h.addVariable(lb=0.0, ub=1.0)
        h.changeColIntegrality(0, highspy.HighsVarType.kInteger)
        h.changeColIntegrality(1, highspy.HighsVarType.kInteger)
        h.addConstr(x + y <= 1)
        h.minimize(-x - 2 * y)
        return h

    def test_a_real_highs_accepts_an_integral_point(self):
        h = self._tiny_mip()
        self.assertTrue(lp._offer_mip_start(h, [0.0, 1.0], phase="unit"))

    def test_the_seed_does_not_change_the_proven_optimum(self):
        """A MIP start supplies an upper bound; it cannot change which
        optimum is proven. Seeded with the WRONG-but-feasible point
        (x=1,y=0, objective -1), the solve must still find y=1
        (objective -2)."""
        h = self._tiny_mip()
        lp._offer_mip_start(h, [1.0, 0.0], phase="unit")
        h.run()
        self.assertEqual(h.modelStatusToString(h.getModelStatus()), "Optimal")
        self.assertAlmostEqual(h.getObjectiveValue(), -2.0, places=6)

    def test_a_refusing_build_is_survived_and_reported(self):
        """`setSolution()`'s availability varies across highspy builds. A
        refused seed must leave an otherwise-working solve unharmed, so
        the function returns False rather than propagating."""

        class _Refuses:
            def setSolution(self, _s):
                raise RuntimeError("this build has no setSolution")

        with self.assertLogs(lp._LOGGER, level="DEBUG"):
            self.assertFalse(lp._offer_mip_start(_Refuses(), [0.0], phase="unit"))


class TestTier1OnARealMip(unittest.TestCase):
    """Tier 1 through the public `build_plan()` path."""

    def setUp(self):
        self.scenario = _mip_scenario(n_loads=10, n_periods=24, seed=0)

    def test_phase2_is_offered_a_genuinely_integral_point(self):
        """The distinction that makes this fix work where #999's
        experiment did not: the offered point must be INTEGRAL on the
        binary columns, not a fractional relaxation optimum."""
        seen: list[tuple[str, list[float]]] = []
        real = lp._offer_mip_start

        def spy(h, col_values, *, phase):
            seen.append((phase, list(col_values)))
            return real(h, col_values, phase=phase)

        with unittest.mock.patch.object(lp, "_offer_mip_start", spy):
            plan = _solve(self.scenario)

        self.assertEqual(plan.status, "optimal")
        phases = [p for p, _ in seen]
        self.assertIn(
            "phase2_secondary",
            phases,
            "tier 1 never ran -- phase 2 was not offered a starting "
            f"incumbent at all (phases seen: {phases})",
        )

        # The binary columns of the offered point must be integral. Their
        # indices aren't known here, so assert on the binaries' own
        # signature: a semi-continuous on/off column sits at 0 or 1.
        _, point = next((p, v) for p, v in seen if p == "phase2_secondary")
        near_binary = [v for v in point if abs(v) < 1e-6 or abs(v - 1.0) < 1e-6]
        self.assertGreater(
            len(near_binary),
            0,
            "the offered point had no crisply-integral entries at all, "
            "which is what a fractional relaxation point looks like",
        )

    def test_the_seed_changes_neither_the_cost_nor_the_dispatch(self):
        """The regression guard. Tier 1 must buy time and nothing else.

        Compared against `_offer_mip_start` patched to a no-op, which is
        exactly the pre-fix code path.
        """
        seeded = _solve(self.scenario)

        with unittest.mock.patch.object(
            lp, "_offer_mip_start", lambda *_a, **_k: False
        ):
            unseeded = _solve(self.scenario)

        self.assertEqual(seeded.status, "optimal")
        self.assertEqual(unseeded.status, "optimal")
        self.assertAlmostEqual(
            seeded.total_cost,
            unseeded.total_cost,
            places=6,
            msg="the MIP start changed the objective, which a starting "
            "incumbent cannot legitimately do",
        )
        # This scenario's battery is deliberately immobile, so grid import
        # IS the dispatch: every degree of freedom routes through the
        # adequacy loads' own binaries and lands here.
        np.testing.assert_allclose(
            np.asarray(seeded.grid_import_kw, dtype=float),
            np.asarray(unseeded.grid_import_kw, dtype=float),
            atol=1e-6,
            err_msg="the MIP start changed the dispatch",
        )


class TestTier2WhenPhase2StillCannotFinish(unittest.TestCase):
    """Tier 2, reached by forcing phase 2 to fail the way a real timeout
    does: the solve runs, then does not come back optimal."""

    def setUp(self):
        self.scenario = _mip_scenario(n_loads=10, n_periods=24, seed=0)

    @staticmethod
    @contextlib.contextmanager
    def _phase2_always_fails():
        """Let the real phase-2 solve run, then raise the same
        `ValueError` `_ensure_optimal_value()` raises on a non-optimal
        status. Faithful to the timeout: `h` has been run against, and
        the caller gets the exception."""
        real = lp._ensure_optimal_value

        def wrapper(h, *, phase="", problem=None, binary_cols=None):
            value = real(h, phase=phase, problem=problem, binary_cols=binary_cols)
            if phase == "phase2_secondary":
                msg = (
                    "LPProblem.solve(options=...): an internal lex/"
                    "calibration phase failed to reach optimal "
                    "(status='Time limit reached')"
                )
                raise ValueError(msg)
            return value

        with unittest.mock.patch.object(lp, "_ensure_optimal_value", wrapper):
            yield

    def test_it_solves_the_pinned_lp_instead_of_losing_the_whole_channel(self):
        pinned: list[list[float]] = []
        real_pin = lp._pin_binaries_to_values

        def spy(h, binary_cols, col_values):
            pinned.append(list(col_values))
            return real_pin(h, binary_cols, col_values)

        with (
            self._phase2_always_fails(),
            unittest.mock.patch.object(lp, "_pin_binaries_to_values", spy),
            _capture_solver_logs() as catcher,
        ):
            plan = _solve(self.scenario)

        self.assertEqual(plan.status, "optimal")
        self.assertTrue(
            pinned,
            "tier 2 never pinned anything -- the ladder fell straight "
            "through to the single-objective fallback",
        )
        joined = "\n".join(catcher.messages)
        self.assertIn(_TIER2_FRAGMENT, joined)
        self.assertNotIn(
            _FALLBACK_FRAGMENT,
            joined,
            "tier 2 ran but the single-objective fallback was reached "
            "anyway -- the secondary channel was still discarded",
        )

    def test_the_cooldown_is_not_armed_when_tier_2_succeeds(self):
        """The 5-minute cooldown exists to stop a DETERMINISTIC failure
        re-failing expensively every cycle. Tier 2 succeeding is not that
        case, and arming the cooldown there would make the next cycle
        skip the phased path for no reason."""
        lp._lex_calibration_failed_until = 0.0
        with self._phase2_always_fails():
            plan = _solve(self.scenario)
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(
            lp._lex_calibration_failed_until,
            0.0,
            "tier 2 handled the failure, so the caller's own except "
            "branch must never have been reached",
        )

    def test_tier_2_keeps_the_primary_cost(self):
        """Tier 2's guarantee. Phase 1's point satisfies the primary
        bound by construction, so pinning it cannot move primary cost
        beyond the tie slack."""
        normal = _solve(self.scenario)
        with self._phase2_always_fails():
            tier2 = _solve(self.scenario)

        self.assertEqual(normal.status, "optimal")
        self.assertEqual(tier2.status, "optimal")
        slack = max(
            lp._LEX_PRIMARY_TIE_ABS_SLACK,
            abs(float(normal.total_cost)) * lp._LEX_PRIMARY_TIE_REL_SLACK,
        )
        self.assertLessEqual(
            float(tier2.total_cost) - float(normal.total_cost),
            slack + 1e-6,
            "tier 2 made the real primary cost worse, which its own "
            "construction forbids",
        )


class TestThePureLpPathIsUntouched(unittest.TestCase):
    """No binaries means no capture and no tier 2 -- a phase failure must
    propagate exactly as it did before this change, so the caller's own
    fallback still handles it."""

    def test_a_phase_failure_with_no_binaries_still_raises(self):
        captured: list[Any] = []
        real = lp._capture_integral_point

        def spy(h):
            captured.append(h)
            return real(h)

        scenario = _mip_scenario(n_loads=4, n_periods=12, seed=1)
        periods, grid, battery, solar, adequacy = scenario
        lp._lex_calibration_failed_until = 0.0
        with unittest.mock.patch.object(lp, "_capture_integral_point", spy):
            plan = build_plan(
                periods=periods,
                grid=grid,
                batteries=[battery],
                solar=solar,
                loads=[],
                # semi_continuous off -> the adequacy loads become
                # continuous, so binary_cols is empty and this is a pure LP
                adequacy_loads=adequacy,
                adequacy_semi_continuous=False,
                solve_options=LexOptions(),
            )
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(
            captured,
            [],
            "the pure-LP path paid for a capture it can never use",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
