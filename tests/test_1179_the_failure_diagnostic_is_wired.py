"""nimbus #1179: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in
tests/v2/solver/test_solve_failure_diagnostic.py."""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from datetime import UTC, datetime

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs
from _writer_source import find_function, writer_source_paths, writer_trees

install_ha_stubs()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver.elements import PeriodGrid

_SRC_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)


def _periods(n: int = 4) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, 0.25), start=datetime(2026, 1, 1, tzinfo=UTC))


def _forced_time_limit_problem() -> lp_mod.LPProblem:
    """A real, genuinely feasible LP, solved under a zero time limit.

    Not a mock: a zero budget is the only way to make real HiGHS return a
    terminal non-optimal status deterministically on every platform, and it
    lands on the same `status != kOptimal` branch #1179's `Unknown`
    failures take. What is being tested is the CAPTURE on that branch, not
    which status got it there.
    """
    p = lp_mod.LPProblem()
    names = [f"x{i}" for i in range(40)]
    for nm in names:
        p.add_variable(nm, lb=0.0, ub=10.0)
        p.set_cost(nm, 1.0)
    for i in range(30):
        p.add_ub_constraint({names[i]: 1.0, names[i + 1]: -1.0}, 2.0, name=f"c{i}")
    p.add_eq_constraint(dict.fromkeys(names, 1.0), 25.0, name="tot")
    return p


def _publish_plan_node() -> ast.FunctionDef:
    # nimbus #1304 (spec 005): this content moved to solver_publish.py.
    # The union of solver_writer.py and every module #1298 has extracted is
    # what this test was always asserting against -- not which file it is in.
    return find_function("publish_plan")


def _failure_warning_call() -> ast.Call:
    """The `_LOGGER.warning(...)` that reports a failed solve.

    Located by its own #757 text rather than by position, so a reordering
    of publish_plan() cannot make this test silently assert on some other
    warning.
    """
    func = _publish_plan_node()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "warning"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "solve did not complete" in str(node.args[0].value)
        ):
            return node
    raise AssertionError("the solve-failure warning was not found in publish_plan()")


def _solve_diagnostics_dict() -> ast.Dict:
    """The `solve_diagnostics` dict literal inside solver_writer.py.

    Parsed rather than imported, because reaching it at runtime means
    driving the whole publish path.
    """
    # nimbus #1304 (spec 005): this content moved to solver_publish.py.
    # The union of solver_writer.py and every module #1298 has extracted is
    # what this test was always asserting against -- not which file it is in.
    tree = ast.Module(
        body=[n for _p, _t in writer_trees() for n in _t.body], type_ignores=[]
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "solve_diagnostics"
                and isinstance(value, ast.Dict)
            ):
                return value
    raise AssertionError("no solve_diagnostics dict literal in solver_writer.py")


class TestTheFieldsExistAndDefaultHonestly(unittest.TestCase):
    """Gap 2, structure. Empty/None on every status but "error", so a
    populated value always means "this cycle failed, and here is what HiGHS
    said" -- never "we looked and found nothing"."""

    def test_an_optimal_plan_carries_no_diagnostic(self):
        """Not merely defaulted -- the real success path must not set them.

        Every `Plan(...)` construction in network.py is checked, not just
        the one in `build_plan()`: that name is a thin wrapper and the real
        optimal construction lives in a helper, which is exactly the kind of
        thing a test that assumed otherwise would silently skip.
        """
        tree = ast.parse((_SRC_DIR / "solver" / "network.py").read_text("utf-8"))
        optimal = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Plan"
            ):
                continue
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            if kwargs.get("status") == "'optimal'":
                optimal.append(kwargs)
        self.assertTrue(optimal, "no Plan(status='optimal') construction found")
        for kwargs in optimal:
            self.assertNotIn("highs_info", kwargs)
            self.assertNotIn("failing_model_path", kwargs)


class TestTheHealthyCycleBaseRateIsPublished(unittest.TestCase):
    """Gap 3. This is the half that makes #1179 falsifiable, and it was
    threaded onto `Plan` and then read by nothing on the success path."""

    def setUp(self):
        self.keys = [
            k.value
            for k in _solve_diagnostics_dict().keys
            if isinstance(k, ast.Constant)
        ]
        self.pairs = {
            k.value: ast.unparse(v)
            for k, v in zip(
                _solve_diagnostics_dict().keys,
                _solve_diagnostics_dict().values,
                strict=True,
            )
            if isinstance(k, ast.Constant)
        }

    def test_a_failed_cycle_is_counted_before_it_returns(self):
        src = (_SRC_DIR / "solver_publish.py").read_text(encoding="utf-8")
        body = src[src.index("def publish_plan(") :]
        branch = body.index("elif plan.solver_failed:")
        counted = body.index('_CYCLE_COUNTS["solver_failures"] += 1')
        returned = body.index("        return\n", branch)
        self.assertLess(branch, counted)
        self.assertLess(counted, returned)

    def test_the_published_key_count_matches_what_sensor_py_documents(self):
        """`_NimbusSolverPushSensor`'s docstring uses this count as a
        staleness fingerprint for a live install, and it read "seven" while
        the code emitted ten -- three keys behind, which would have read a
        CURRENT install as stale. Pinned to the source so they cannot drift
        apart in silence again."""
        doc = (_SRC_DIR / "sensor.py").read_text(encoding="utf-8")
        words = {
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
            17: "seventeen",
            18: "eighteen",
            19: "nineteen",
            20: "twenty",
            21: "twenty-one",
            22: "twenty-two",
            23: "twenty-three",
        }
        n = len(self.keys)
        self.assertIn(n, words, f"extend the number words for {n} keys")
        self.assertIn(
            f"emits **{words[n]}**",
            doc,
            f"solve_diagnostics emits {n} keys; sensor.py's staleness "
            f"fingerprint does not say {words[n]}",
        )


class TestNothingBranchesOnAnyOfIt(unittest.TestCase):
    """Observability only, enforced the way #1229's own control test does
    it -- by walking the AST of every condition rather than asserting it in
    prose."""

    _FIELDS = ("highs_info", "failing_model_path")

    def _tests_of_every_conditional(self, path: pathlib.Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                out.append(ast.unparse(node.test))
        return out

    def test_the_only_branch_anywhere_is_the_log_guard(self):
        """solver_writer.py legitimately tests `failing_model_path` once, to
        decide whether to print a path it does not have. Exactly once."""
        # nimbus #1304 (spec 005): publish_plan, which holds the one
        # legitimate branch, moved to solver_publish.py. Scan every module the
        # writer has been split into -- "branches nowhere in the writer" is the
        # claim, not "branches nowhere in one file".
        tests = [
            t
            for path in writer_source_paths()
            for t in self._tests_of_every_conditional(path)
        ]
        hits = [t for t in tests if any(f in t for f in self._FIELDS)]
        self.assertEqual(
            hits,
            ["plan.failing_model_path is not None"],
            f"unexpected branching on a #1179 diagnostic: {hits}",
        )
