"""nimbus issue #773: per-call timing for every HiGHS solve invocation.

Added after three separate hypotheses for that issue's `"Time limit
reached"` failures were each measured and refuted — a large fleet pushing
the lex phase infeasible, wall-clock contention between concurrent solves,
and the offer-curve ranging walk. What survived all three is the shape:
on a real install a whole solve cycle takes **0.6s** and the entire
offer-curve sweep takes **47ms**, while roughly once a minute a single
HiGHS call runs past the 60s per-call limit. Same code, same problem
shape, three orders of magnitude apart.

That cannot be diagnosed from outside the process, so this is a
measurement rather than a fourth theory.

The field that actually discriminates is `simplex_iterations`: a stalled
call with a huge iteration count is degeneracy/cycling, while one with a
small count is stuck somewhere that is not the simplex loop at all
(presolve, a MIP tree, numerics). Those point at different fixes, and
nothing previously logged could tell them apart — so the tests below pin
that the field is actually carried, not just that *a* warning fires.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import MagicMock

import _solver_path
from solver import lp


class _FakeHighs:
    """Only the three reads the diagnostic performs."""

    def __init__(
        self, iterations=12345, status="Optimal", explode=False, nodes=7, gap=0.5
    ):
        self._iterations = iterations
        self._status = status
        self._explode = explode
        self._nodes = nodes
        self._gap = gap

    def getInfo(self):
        if self._explode:
            raise RuntimeError("highs info unavailable")
        return MagicMock(
            simplex_iteration_count=self._iterations,
            mip_node_count=self._nodes,
            mip_gap=self._gap,
        )

    def getModelStatus(self):
        return object()

    def modelStatusToString(self, _status):
        return self._status


class _Clock:
    """Deterministic stand-in for time.monotonic()."""

    def __init__(self, *ticks):
        self._ticks = list(ticks)

    def __call__(self):
        return self._ticks.pop(0) if len(self._ticks) > 1 else self._ticks[0]


class TestThreshold(unittest.TestCase):
    def setUp(self):
        self._real_monotonic = lp.time.monotonic
        self.addCleanup(lambda: setattr(lp.time, "monotonic", self._real_monotonic))

    def _run(self, elapsed, **kwargs):
        """Returns (warning_lines, debug_lines) for one timed call."""
        lp.time.monotonic = _Clock(100.0, 100.0 + elapsed)
        with self.assertLogs(lp._LOGGER, level="DEBUG") as caught:
            # A guaranteed record so assertLogs never fails for the
            # "nothing was logged" reason when asserting absence.
            lp._LOGGER.warning("sentinel")
            with lp._timed_lp_call(_FakeHighs(**kwargs), "primary_minimize"):
                pass
        lines = [r for r in caught.output if "sentinel" not in r]
        return (
            [r for r in lines if r.startswith("WARNING")],
            [r for r in lines if r.startswith("DEBUG")],
        )

    def test_a_fast_call_logs_nothing(self):
        """The common case by an enormous margin -- a healthy install must
        stay silent, or this diagnostic is just noise."""
        warns, debugs = self._run(0.6)
        self.assertEqual(warns, [])
        self.assertEqual(debugs, [])

    def test_a_call_just_under_the_threshold_logs_nothing(self):
        warns, debugs = self._run(lp._SLOW_LP_CALL_SECONDS - 0.01)
        self.assertEqual(warns, [])
        self.assertEqual(debugs, [])

    def test_a_slow_but_successful_call_is_debug_not_warning(self):
        """nimbus issue #773: the 5s line answered its question in a day,
        then fired roughly once a MINUTE on a real install for a condition
        now understood (expensive root relaxation, mip_node_count=1). A
        permanent warning for a known, non-actionable condition is the
        same log noise v0.94.297 had to clean up for #757 -- so this is
        still measured and still logged, just not as an alarm."""
        warns, debugs = self._run(lp._SLOW_LP_CALL_SECONDS + 0.01)
        self.assertEqual(warns, [])
        self.assertEqual(len(debugs), 1)
        self.assertIn("#773 diag", debugs[0])

    def test_a_call_past_the_alarming_threshold_warns(self):
        """Half the per-call limit -- the point where the next slightly
        harder instance starts timing out for real."""
        warns, _debugs = self._run(lp._ALARMING_LP_CALL_SECONDS + 0.1)
        self.assertEqual(len(warns), 1)
        self.assertIn("#773 diag", warns[0])

    def test_a_non_optimal_call_warns_however_brief(self):
        """Status matters independently of duration: a call that did not
        reach optimal is always worth an alarm, even a quick one."""
        warns, _debugs = self._run(
            lp._SLOW_LP_CALL_SECONDS + 0.01, status="Time limit reached"
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("Time limit reached", warns[0])

    def test_the_alarming_threshold_is_below_the_hard_limit(self):
        """A threshold at or above the per-call limit could only ever fire
        on a call that had already failed, which is too late to be a
        warning about anything."""
        self.assertLess(lp._ALARMING_LP_CALL_SECONDS, lp.DEFAULT_TIME_LIMIT_SECONDS)
        self.assertGreater(lp._ALARMING_LP_CALL_SECONDS, lp._SLOW_LP_CALL_SECONDS)

    def test_the_warning_carries_the_mip_node_count(self):
        """nimbus issue #773: the field that separated the last two
        explanations. A large node count means branch-and-bound is
        exploring a huge tree; 0 or 1 means the solver never left the
        ROOT and the LP relaxation itself is what is slow."""
        warns, _d = self._run(61.0, nodes=48211)
        self.assertIn("mip_node_count=48211", warns[0])

    def test_a_root_bound_stall_is_distinguishable(self):
        warns, _d = self._run(61.0, nodes=0)
        self.assertIn("mip_node_count=0", warns[0])

    def test_the_warning_carries_the_mip_gap(self):
        warns, _d = self._run(61.0, gap=0.0123)
        self.assertIn("mip_gap=0.0123", warns[0])

    def test_the_warning_names_the_call_site(self):
        """Ten call sites exist; a timing line that doesn't say which one
        it measured would not have narrowed anything."""
        warns, _d = self._run(61.0)
        self.assertIn("primary_minimize", warns[0])

    def test_the_discriminating_field_is_carried(self):
        warns, _d = self._run(61.0, iterations=987654)
        self.assertIn("simplex_iterations=987654", warns[0])

    def test_the_elapsed_time_is_reported(self):
        warns, _d = self._run(61.5)
        self.assertIn("61.5s", warns[0])


class TestRobustness(unittest.TestCase):
    def setUp(self):
        self._real_monotonic = lp.time.monotonic
        self.addCleanup(lambda: setattr(lp.time, "monotonic", self._real_monotonic))

    def test_a_raising_call_is_still_timed_and_reported(self):
        """A solve that blows up after 55 seconds is at least as
        interesting as one that returns slowly, and the exception itself
        carries no timing."""
        lp.time.monotonic = _Clock(0.0, 55.0)
        with (
            self.assertLogs(lp._LOGGER, level="WARNING") as caught,
            self.assertRaises(ValueError),
            lp._timed_lp_call(_FakeHighs(), "lex_phase"),
        ):
            raise ValueError("boom")
        self.assertTrue(any("#773 diag" in line for line in caught.output))
        self.assertTrue(any("lex_phase" in line for line in caught.output))

    def test_the_original_exception_is_not_swallowed(self):
        lp.time.monotonic = _Clock(0.0, 55.0)
        with (
            self.assertLogs(lp._LOGGER, level="WARNING"),
            self.assertRaises(KeyError),
            lp._timed_lp_call(_FakeHighs(), "lex_phase"),
        ):
            raise KeyError("original")

    def test_an_unreadable_info_struct_still_produces_the_line(self):
        """Best-effort reads: a diagnostic must never be the reason a real
        solve cycle dies, and elapsed + label are worth having alone."""
        lp.time.monotonic = _Clock(0.0, 70.0)
        with (
            self.assertLogs(lp._LOGGER, level="WARNING") as caught,
            lp._timed_lp_call(_FakeHighs(explode=True), "sweep_cost"),
        ):
            pass
        self.assertTrue(any("70.0s" in line for line in caught.output))
        self.assertTrue(
            any("simplex_iterations=None" in line for line in caught.output)
        )

    def test_a_fast_call_never_touches_the_info_struct(self):
        """The overhead on the hot path must be one clock read and a
        comparison — nothing else, since this wraps every solve."""
        lp.time.monotonic = _Clock(0.0, 0.1)
        h = _FakeHighs(explode=True)  # would raise if read
        with lp._timed_lp_call(h, "primary_minimize"):
            pass


class TestEverySolveCallIsTimed(unittest.TestCase):
    """The helper can be perfectly correct while a new call site is added
    without it. This walks lp.py's own AST so that cannot happen quietly.
    """

    @staticmethod
    def _tree():
        src = pathlib.Path(
            _solver_path._SOLVER_PARENT  # type: ignore[attr-defined]
        ).joinpath("solver", "lp.py")
        return ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    def test_no_untimed_highs_solve_invocation_exists(self):
        tree = self._tree()

        timed: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            if not any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "_timed_lp_call"
                for item in node.items
            ):
                continue
            for inner in ast.walk(node):
                timed.add(id(inner))

        untimed = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "minimize")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "h"
            ):
                continue
            if id(node) not in timed:
                untimed.append(f"h.{node.func.attr}() at line {node.lineno}")

        self.assertEqual(
            untimed,
            [],
            "every HiGHS solve invocation must be wrapped in "
            "_timed_lp_call() -- an untimed one is invisible to nimbus "
            "issue #773's own diagnostic, which is the whole reason it "
            "was added",
        )

    def test_the_guard_would_actually_catch_an_untimed_call(self):
        """Guard the guard: a test that can only ever pass proves
        nothing. This asserts the detector fires on a synthetic untimed
        call."""
        tree = ast.parse("def f(h):\n    h.run()\n")
        found = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
        ]
        self.assertEqual(len(found), 1)

    def test_no_call_site_reports_a_hardcoded_problem_size(self):
        """Caught during review of this very change: `primary_minimize`
        was first wired with `n_binary=0`, which reads as a real
        measurement and is a lie whenever the options=None path solves a
        MIP (`binary_cols` is in scope there and can be non-empty).

        A hardcoded size is worse than `None` -- `None` says "not
        known", a literal says "measured, and it is this". Pinned so a
        future call site cannot quietly reintroduce one.
        """
        tree = self._tree()
        offenders = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_timed_lp_call"
            ):
                continue
            for kw in node.keywords:
                if kw.arg not in ("n_vars", "n_binary"):
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is not None:
                    offenders.append(
                        f"{kw.arg}={kw.value.value!r} at line {node.lineno}"
                    )
        self.assertEqual(
            offenders,
            [],
            "a problem-size argument must be read from the real problem "
            "or left as None -- a hardcoded literal reports a measurement "
            "that was never taken",
        )

    def test_both_run_and_minimize_are_covered(self):
        """`h.minimize()` is the options=None primary solve and is easy to
        miss when only `h.run()` is grepped for -- it was, until #773's
        own investigation found it."""
        tree = self._tree()
        attrs = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "h"
            and n.func.attr in ("run", "minimize")
        }
        self.assertEqual(attrs, {"run", "minimize"})


if __name__ == "__main__":
    unittest.main()
