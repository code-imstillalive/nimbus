"""A failed solve cycle records what is needed to diagnose it, in its own log
line (#1179) (#1837).
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys
import unittest
from datetime import UTC, datetime

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs
from _writer_source import find_function, writer_trees

install_ha_stubs()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod
from custom_components.nimbus_load.solver.elements import PeriodGrid

_SRC_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "nimbus_load"
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


class TestTheFieldsExistAndDefaultHonestly(unittest.TestCase):
    """Gap 2, structure. Empty/None on every status but "error", so a
    populated value always means "this cycle failed, and here is what HiGHS
    said" -- never "we looked and found nothing"."""

    def test_lpresult_carries_both(self):
        r = lp_mod.LPResult(status="optimal")
        self.assertEqual(r.highs_info, {})
        self.assertIsNone(r.failing_model_path)

    def test_plan_mirrors_both(self):
        f = {x.name: x for x in dataclasses.fields(net_mod.Plan)}
        for name in ("highs_info", "failing_model_path"):
            self.assertIn(name, f, f"Plan must mirror LPResult.{name}")
        self.assertIsNone(f["failing_model_path"].default)
        # A mutable default must come from a factory, or every Plan in the
        # process shares one dict -- the exact class of aliasing bug #356
        # already found and fixed across eight of this dataclass's arrays.
        self.assertIs(f["highs_info"].default, dataclasses.MISSING)
        self.assertIs(f["highs_info"].default_factory, dict)


class TestInfeasiblePlanPropagatesThem(unittest.TestCase):
    """Gap 2, the hop a failing cycle actually takes."""

    def test_the_dict_is_not_aliased_to_the_callers(self):
        """Same reasoning as #356's `np.zeros(n)`-per-field fix: a caller
        mutating its own dict afterwards must not reach into a frozen Plan."""
        caller = {"simplex_iteration_count": 0.0}
        plan = net_mod._infeasible_plan(
            _periods(), "error", iterations=0, highs_info=caller
        )
        caller["simplex_iteration_count"] = 999.0
        self.assertEqual(plan.highs_info, {"simplex_iteration_count": 0.0})


class TestTheFormatter(unittest.TestCase):
    def test_integral_values_print_without_a_decimal_point(self):
        out = lp_mod.format_highs_info({"simplex_iteration_count": 0.0})
        self.assertEqual(out, "simplex_iterations=0")

    def test_the_discriminating_field_is_first(self):
        out = lp_mod.format_highs_info(
            {"mip_node_count": 1.0, "simplex_iteration_count": 0.0}
        )
        self.assertTrue(out.startswith("simplex_iterations=0"), out)


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


class TestTheFailureLineNamesEverything(unittest.TestCase):
    """Gap 1. A field nothing reports is not observability -- the same
    argument #1291 made about `calibration_weight_used`, one hop further
    out."""

    def setUp(self):
        self.call = _failure_warning_call()
        self.arg_src = [ast.unparse(a) for a in self.call.args]
        self.template = str(self.call.args[0].value)

    def test_it_reports_the_iteration_count(self):
        self.assertIn(
            "plan.iterations",
            self.arg_src,
            "the failure warning must print the count lp.py calls the "
            "discriminating field -- #1179's central open question",
        )
        self.assertIn("simplex_iterations=", self.template)

    def test_it_reports_the_highs_snapshot(self):
        self.assertTrue(
            any("format_highs_info(plan.highs_info)" in a for a in self.arg_src),
            f"HiGHS's own numbers are missing from the failure line: {self.arg_src}",
        )

    def test_it_reports_where_the_model_went(self):
        self.assertTrue(
            any("failing_model_path" in a for a in self.arg_src),
            "the failure line must say whether the offending model was "
            "captured, and where",
        )

    def test_it_still_reports_the_weight_and_the_status(self):
        """The regression half: #1229/#1260/#1291 must not be lost."""
        self.assertIn("plan.raw_status or 'unknown reason'", self.arg_src)
        self.assertIn("calibration_note", self.arg_src)
        self.assertIn("nimbus issue #1179", self.template)

    def test_the_elapsed_time_keeps_enough_resolution_to_show_the_signature(self):
        """0.1s against 1.16s IS the signature; `%.1f` is at the edge of its
        own resolution there."""
        self.assertIn("%.3fs", self.template)
        self.assertNotIn("%.1fs", self.template)

    def test_the_perishability_note_is_only_emitted_when_a_file_exists(self):
        """#773 learned this the hard way (see `_dump_location_note()`'s own
        docstring): announcing a dump without saying it is temporary loses
        the artefact. Announcing one that was never written is worse."""
        func = _publish_plan_node()
        notes = [
            n
            for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "warning"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and "the failing model is at" in str(n.args[0].value)
        ]
        self.assertEqual(len(notes), 1, "expected exactly one capture-location line")
        guarded = [
            n
            for n in ast.walk(func)
            if isinstance(n, ast.If)
            and "failing_model_path" in ast.unparse(n.test)
            and notes[0] in list(ast.walk(n))
        ]
        self.assertTrue(
            guarded,
            "the capture-location line must sit under an `if plan."
            "failing_model_path is not None` guard",
        )


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

    def test_all_three_calibration_fields_are_published(self):
        for name in (
            "calibration_min_weight_fallback",
            "calibration_fallback_reason",
            "calibration_weight_used",
        ):
            self.assertIn(
                name,
                self.keys,
                f"{name} is on Plan for both outcomes but published on "
                "neither -- the healthy-cycle base rate #1229 and #1291 "
                "both rest on is unobservable without this",
            )
            self.assertEqual(self.pairs[name], f"plan.{name}")

    def test_the_healthy_iteration_count_is_published(self):
        """Measured while building this: a small LP solves optimally at
        `simplex_iteration_count == 0`, because presolve finishes it. So a
        failing cycle's 0 is only a rejection relative to what THIS
        install's healthy cycles cost."""
        self.assertIn("simplex_iterations", self.keys)
        self.assertEqual(self.pairs["simplex_iterations"], "plan.iterations")

    def test_the_cycle_counters_are_published(self):
        """nimbus #1360: a failed cycle publishes nothing and its warning is
        lost at the next restart, and since the one-minute cadence a
        missing history row no longer means a missing solve. The counters
        carried on every healthy row are what make an episode readable."""
        self.assertEqual(
            self.pairs["solves_attempted"], "_CYCLE_COUNTS['solves_attempted']"
        )
        self.assertEqual(
            self.pairs["solver_failures"], "_CYCLE_COUNTS['solver_failures']"
        )

    def test_the_key_name_matches_the_one_the_failure_line_prints(self):
        """Read side by side, so no translation step stands between the
        failing value and its baseline."""
        self.assertIn("simplex_iterations=", str(_failure_warning_call().args[0].value))
        self.assertIn("simplex_iterations", self.keys)


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

    def test_the_solver_never_decides_anything_on_them(self):
        for name in ("lp.py", "network.py"):
            path = _SRC_DIR / "solver" / name
            for test_src in self._tests_of_every_conditional(path):
                for field in self._FIELDS:
                    self.assertNotIn(
                        field,
                        test_src,
                        f"{name} branches on {field} (`{test_src}`) -- these "
                        "are diagnostics; a solve must be byte-identical "
                        "whether or not they were captured",
                    )


if __name__ == "__main__":
    unittest.main()
