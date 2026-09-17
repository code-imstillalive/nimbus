"""nimbus #773: `phase3_lex_restore` is a SECOND pinned LP re-solve, and
it was re-certifying MIP-accepted binaries at the tighter LP tolerance.

`_lp_tolerance_matching_mip()`'s own docstring claimed, until 2026-09-18,
that `phase2_pin_resolve` was *"the single place asking an LP to
re-certify a point a MIP already accepted"*. It was not. Phase 3 runs
after the same `_pin_binaries_to_current_solution()` call, so its binaries
are pinned to values branch-and-bound accepted under
`mip_feasibility_tolerance` (1e-06), and it then asked a pure LP to
reproduce them under `primal_feasibility_tolerance` (1e-07) -- ten times
tighter, the exact band #979 was written for.

**Deterministic reproduction, with both shipped #773 fixes in place.**
This is not a scenario built by monkeypatching a fix to a no-op; it fails
on unmodified current code:

    n_loads=16, n_periods=24, seed=0, LexOptions()
        phase3_lex_restore  status='Infeasible'
        max_primal_infeasibility  2.812066e-07      <- inside 1e-7..1e-6
        num_primal_infeasibilities  1

    n_loads=24, n_periods=48, seed=0, LexOptions()
        same failure, max_primal_infeasibility 2.812066e-07

Two different problem sizes agreeing to seven significant figures is the
signature of one numerical mechanism rather than two coincidences.

**The mechanism was established by elimination, not assumption.** Phase 3
also adds a `secondary_expr <= secondary_value + epsilon` row, which is
the #981 hazard class -- a bound derived from a solver-reported value,
capable of cutting off the point it came from. That was the competing
explanation and it is wrong: widening that epsilon **one hundred fold**
left the failure bit-identical, same 2.812066e-07 and same single
violated row. So the secondary row is not what is violated; the pinned
column bounds are, and the tolerance is the fix.

**Scope, stated plainly.** Phase 3 runs only under `LexOptions`.
Production (`solver_writer.py`) passes `CalibratedOptions`, which returns
before phase 3, so **this is not the production #773 failure** and fixing
it does not close that. It is a real latent defect in a supported mode,
with the first deterministic end-to-end reproduction this issue has had
that does not require disabling a fix first -- which is the gap #999
filed.

**Why the plan barely moves.** Before the fix the failure fell through to
`network.py`'s fallback, which produces a valid plan by a plain
single-objective solve -- so `Plan.status` reads `"optimal"` either way,
and the real signal is the log line, exactly as #999 records for the
sibling case. Measured primary cost shifts by **3.8e-07 dollars** on the
two affected scenarios (four ten-millionths of a cent, i.e. the tolerance
band itself) and is **bit-identical** on a scenario that never failed.
What actually changes is that the lex tie-break guarantee stops being
silently skipped on those cycles.
"""

from __future__ import annotations

import contextlib
import logging
import unittest

import _solver_path  # noqa: F401
from solver import lp
from solver.lp import CalibratedOptions, LexOptions
from test_773_tolerance_and_tie_slack_end_to_end import (
    _many_binaries_tied_price_scenario,
    _solve,
)

# The two (n_loads, n_periods, seed) combinations found to fail
# deterministically on unmodified pre-fix code, and one that never did.
FAILING = ((16, 24, 0), (24, 48, 0))
CLEAN = ((16, 24, 1), (10, 16, 0))


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _captured_logs():
    handler = _CapturingHandler()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _phase3_failed(messages: list[str]) -> bool:
    return any(
        "phase3_lex_restore" in m and "failed to reach optimal" in m for m in messages
    )


def _run(n_loads: int, n_periods: int, seed: int):
    # Same cooldown reset as the sibling file: lp's own
    # _lex_calibration_failed_until would otherwise let one test's
    # failure silently skip the phased path in a later one.
    lp._lex_calibration_failed_until = 0.0
    scenario = _many_binaries_tied_price_scenario(
        n_loads=n_loads, n_periods=n_periods, seed=seed
    )
    with _captured_logs() as handler:
        plan = _solve(*scenario, options=LexOptions())
    return plan, handler.messages


class TestTheFixHoldsOnTheFailingScenarios(unittest.TestCase):
    """Forward direction: with the fix in place, nothing fails."""

    def test_no_phase3_failure_on_the_reproducing_scenarios(self):
        for n_loads, n_periods, seed in FAILING:
            with self.subTest(n_loads=n_loads, n_periods=n_periods, seed=seed):
                plan, messages = _run(n_loads, n_periods, seed)
                self.assertFalse(
                    _phase3_failed(messages),
                    f"phase3_lex_restore failed on ({n_loads}, {n_periods}, "
                    f"{seed}) -- the pinned LP is being re-certified at the "
                    "tighter tolerance again; see _lp_tolerance_matching_mip()",
                )
                self.assertEqual(plan.status, "optimal")


class TestTheFixIsWhatIsDoingIt(unittest.TestCase):
    """Reverse direction. Without it this file would only prove these
    scenarios pass, not that the fix is why -- exactly the gap #999 filed
    about the two fixes shipped before it.

    **Neutralising the helper globally does not work, and finding out why
    sharpened the whole result.** Doing that makes `phase2_pin_resolve`
    fail FIRST, at the identical `max_primal_infeasibility=2.812066e-07`,
    and `network.py`'s fallback then returns before phase 3 is ever
    reached -- so phase 3 "passes" for the worst possible reason.

    That is not noise, it is the finding restated: this scenario pins one
    numerical point that gets re-certified by an LP **twice**, once per
    pinned re-solve. #979 guarded the first. The second was left open,
    which is what this file is about. A global neutralisation cannot
    separate them, so only the phase-3 call site is neutralised here --
    identified as the second invocation within a solve, since
    `phase2_pin_resolve` always precedes it whenever `binary_cols` is
    non-empty (and it is, in every scenario here: 768 and 2304 binaries).
    """

    def test_neutralising_only_the_phase3_call_site_brings_it_back(self):
        original = lp._lp_tolerance_matching_mip
        calls = {"n": 0}

        @contextlib.contextmanager
        def _noop_after_the_first(h):
            calls["n"] += 1
            if calls["n"] == 1:
                # phase2_pin_resolve -- leave #979's fix intact, or it
                # fails there and the fallback hides phase 3 entirely.
                with original(h):
                    yield
            else:
                yield  # phase3_lex_restore, pre-fix behaviour

        reproduced = []
        lp._lp_tolerance_matching_mip = _noop_after_the_first
        try:
            for n_loads, n_periods, seed in FAILING:
                calls["n"] = 0
                _plan, messages = _run(n_loads, n_periods, seed)
                reproduced.append(_phase3_failed(messages))
        finally:
            lp._lp_tolerance_matching_mip = original

        self.assertTrue(
            all(reproduced),
            "with only the phase-3 call site neutralised, "
            "phase3_lex_restore no longer fails on scenarios that "
            f"reproduced it deterministically -- got {reproduced}. Either "
            "they have stopped reaching the 1e-7..1e-6 band (a HiGHS "
            "version change, or the model shape moved), or something else "
            "now compensates. This test proves nothing until a new "
            "reproducing scenario is found, so do not simply delete it.",
        )

    def test_the_patch_target_is_the_name_the_call_sites_resolve(self):
        """The monkeypatch above only works while both call sites look
        the helper up as a module global at call time. If either is ever
        rebound to a local alias, the reverse test would silently stop
        neutralising anything and start passing vacuously."""
        source = lp.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("with _lp_tolerance_matching_mip(h):"), 2)
        self.assertNotIn("_tolerance_cm = _lp_tolerance_matching_mip", text)

    def test_the_helper_is_restored_afterwards(self):
        """The neutralisation above is a monkeypatch on a module global;
        if its restore ever regressed, every later test in the process
        would run without the fix and this file would start passing for
        the wrong reason."""
        self.assertTrue(hasattr(lp._lp_tolerance_matching_mip, "__wrapped__"))


class TestScenariosThatNeverFailedAreUntouched(unittest.TestCase):
    """The fix must be a strict no-op where the condition does not arise.
    Measured: primary cost is bit-identical on these."""

    def test_clean_scenarios_stay_clean(self):
        for n_loads, n_periods, seed in CLEAN:
            with self.subTest(n_loads=n_loads, n_periods=n_periods, seed=seed):
                plan, messages = _run(n_loads, n_periods, seed)
                self.assertFalse(_phase3_failed(messages))
                self.assertEqual(plan.status, "optimal")


class TestBothPinnedResolvesAreCovered(unittest.TestCase):
    """The defect was that one of the two pinned LP re-solves had the
    guard and the other did not. A source check, because the asymmetry is
    what regresses -- and because the whole finding is that reading the
    code once was not enough to notice it.
    """

    def test_the_helper_wraps_two_call_sites_not_one(self):
        source = lp.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(
            text.count("with _lp_tolerance_matching_mip(h):"),
            2,
            "expected both pinned LP re-solves (phase2_pin_resolve and "
            "phase3_lex_restore) to run under the MIP-matched tolerance -- "
            "a third pinned re-solve added later needs the same treatment, "
            "and dropping one silently reintroduces #773's band failure",
        )


if __name__ == "__main__":
    unittest.main()


class TestTheCalibratedPathIsWorse(unittest.TestCase):
    """**The production path.** `solver_writer.py` passes
    `CalibratedOptions`, and the same pinned-LP band bites it harder than
    it bites phase 3 -- with none of the safety net.

    `_calibrate_blend_weight()` runs its probes and its final solve with
    the binaries still pinned, through raw `h.run()` rather than
    `_ensure_optimal_value()`. `_primary_acceptable()` then treats a
    non-optimal status as *"this weight is unacceptable"* and returns
    False. So a probe that goes Infeasible inside the tolerance band does
    not raise, does not log a #773 diagnostic, and does not reach
    `network.py`'s fallback. It reads as evidence that no blend weight
    works.

    Measured on `main` at v0.94.385, n_loads=16/n_periods=24/seed=0:

        3 of 6 HiGHS runs returned kInfeasible
        WARNING  "no blend weight preserves primary cost within
                  tolerance (2.45e-03); using minimum weight 1.00e-12"
        Plan.status  "infeasible"        <- dispatch goes unavailable
        total_cost   None

    against `LexOptions` returning `optimal` on the identical model. A
    model that solves under one tie-break mode and is reported infeasible
    under another is the strongest available evidence that the mode, not
    the model, is at fault.

    This is the part that matters for #757's symptom class: the
    documented promise is *"falling back to a plain single-objective
    solve so dispatch doesn't go unavailable"*, and here dispatch goes
    unavailable **without that fallback ever running**, because nothing
    raised.
    """

    def test_the_calibrated_path_solves_the_model_it_used_to_call_infeasible(self):
        for n_loads, n_periods, seed in FAILING:
            with self.subTest(n_loads=n_loads, n_periods=n_periods, seed=seed):
                lp._lex_calibration_failed_until = 0.0
                scenario = _many_binaries_tied_price_scenario(
                    n_loads=n_loads, n_periods=n_periods, seed=seed
                )
                plan = _solve(*scenario, options=CalibratedOptions())
                self.assertEqual(
                    plan.status,
                    "optimal",
                    "the CalibratedOptions path reports this model infeasible "
                    "again. It is not: LexOptions solves it. Dispatch goes "
                    "unavailable here with NO #773 diagnostic and without "
                    "network.py's fallback firing, because "
                    "_calibrate_blend_weight() swallows a non-optimal probe "
                    "as 'this weight is unacceptable' rather than raising",
                )
                self.assertIsNotNone(plan.total_cost)

    def test_both_tie_break_modes_now_agree_on_the_same_model(self):
        """The sharpest assertion available: one model, two modes, one
        answer. Before the fix these differed by `optimal` vs
        `infeasible`."""
        lp._lex_calibration_failed_until = 0.0
        scenario = _many_binaries_tied_price_scenario(n_loads=16, n_periods=24, seed=0)
        lex = _solve(*scenario, options=LexOptions())
        lp._lex_calibration_failed_until = 0.0
        cal = _solve(*scenario, options=CalibratedOptions())
        self.assertEqual(lex.status, cal.status)
        self.assertAlmostEqual(float(lex.total_cost), float(cal.total_cost), places=9)

    def test_every_pinned_run_in_the_calibration_search_is_covered(self):
        """`_calibrate_blend_weight` has three `h.run()` sites and all
        three operate on pinned binaries. Wrapping only the probe left
        the FINAL blended solve infeasible -- measured -- so this pins
        that all three stay wrapped. A fourth added later needs it too.
        """
        source = lp.__file__.replace(".pyc", ".py")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        for label in (
            "calibrate_blend_probe",
            "primary_acceptable_probe",
            "calibrate_blend_final",
        ):
            with self.subTest(call_site=label):
                self.assertIn(
                    f'with _lp_tolerance_matching_mip(h), _timed_lp_call(h, "{label}")',
                    text,
                    f"the {label} solve runs on pinned binaries but no "
                    "longer matches the MIP tolerance they were accepted "
                    "under -- this is what made the production path report "
                    "a solvable model as infeasible",
                )


class TestWhatThisDoesNotFix(unittest.TestCase):
    """Scope, pinned so it is not overstated later.

    Matching the LP tolerance to the MIP tolerance can only rescue a
    violation INSIDE the 1e-7..1e-6 band. A larger one is still a genuine
    infeasibility for both.

    Measured, with this fix applied, n_loads=20/n_periods=32/seed=3:

        phase3_lex_restore  max_primal_infeasibility  1.919759e-06

    which is ABOVE `mip_feasibility_tolerance` itself, so widening the
    primal tolerance to match cannot help by construction. That scenario
    still reports `infeasible` on the calibrated path and still falls
    back on the lex path. It is a real, separate, still-open case and it
    is recorded here rather than left for someone to rediscover as a
    regression in this fix.
    """

    def test_the_above_band_scenario_is_still_unresolved(self):
        lp._lex_calibration_failed_until = 0.0
        scenario = _many_binaries_tied_price_scenario(n_loads=20, n_periods=32, seed=3)
        plan = _solve(*scenario, options=CalibratedOptions())
        self.assertEqual(
            plan.status,
            "infeasible",
            "the above-band scenario (1.92e-06, outside the tolerance gap) "
            "now solves. That is good news and this test is the wrong "
            "shape for it -- find out WHAT fixed it, then update this "
            "file's scope section rather than only flipping the assertion",
        )
