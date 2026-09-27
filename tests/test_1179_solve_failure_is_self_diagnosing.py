"""nimbus #1179: a failed solve cycle records everything needed to diagnose
itself, in its own log line, instead of only that it failed.

**The state this closes.** On 2026-09-20 the reference household produced
**153 failed solve cycles in 2.7 hours** inside the P2P window, publishing
no plan (#757's guard, correctly). Each failure completed in **0.1s against
a 1.16s healthy solve** and carried `raw_status="Unknown"` -- HiGHS
declining to say what happened.

Re-measured 2026-09-27 with an instrument that does not depend on the log at
all: only SUCCESSFUL solves push `sensor.nimbus_solver_solve_seconds`, so its
per-hour state-change count is a failure detector. Over **2026-09-18 00:00 ->
2026-09-27 18:00 AEST, 233 full hours, median 214 successes/hour**, the only
hours below 85% of median are **2026-09-20 21:00 through 2026-09-21 02:00**
(162 / 145 / 159 / **131** / 164 / 140). Two readings:

- **the episode was ~6 hours, not 2.7**, and its worst hour was 00:00 -- after
  the P2P window closed. #1179 records only "4 failures in the first 3 minutes"
  of that hour, which was its log window ending, the same artefact its own
  author had already caught for the 23:00 hour.
- **no recurrence** in the 159 full hours after 2026-09-21 03:00. The lowest
  are the documented 06:00 retrain hours (185-193, 86-90%).

So the episode cannot be diagnosed retrospectively, and #1179 is by now a
question about what the NEXT one records.

**Three gaps, each measured against the code rather than argued.**

1. **`Plan.iterations` never reached the failure line.** `solver/lp.py`'s
   own comment above `_SLOW_LP_CALL_SECONDS` names it as the field that
   decides this exact question -- "a stalled call with a huge iteration
   count is degeneracy/cycling, while a stalled call with a small one is
   stuck somewhere that is not the simplex loop at all -- presolve, a MIP
   branch-and-bound tree, or numerical trouble" -- and
   `_solve_with_options()`'s own docstring records having seen "an instant
   presolve rejection with zero simplex iterations" on an intermediate
   phase. `_infeasible_plan()` has carried the count since #356. The one
   warning that reports the failure printed the elapsed time and the status
   string and stopped.

2. **The terminal `status="error"` exit captured nothing from HiGHS and
   dumped no model.** `_run_phase()` does both for an INTERMEDIATE phase
   failure. The terminal path -- the one these failures take, and the only
   one whose failure skips the publish entirely -- was the single
   non-optimal exit in `lp.py` with neither.

3. **The healthy-cycle base rate was not observable at all.** #1229 and
   #1291 both argue that "the failures took the minimum-weight fallback"
   is unfalsifiable without it, and both state the calibration fields are
   carried on success too. They are carried onto `Plan`; nothing read them
   there. All three appear in `solver_writer.py` only inside the failure
   branch, so on a deployed install a healthy cycle's weight went nowhere.

**Why published and not just logged.** Production's `/api/error_log` is
truncated at every container restart: read 2026-09-27 18:18 AEST it began at
16:09:58 AEST, the boot 2h 8m earlier. A log-only record of something that
recurs every few nights is routinely gone before anyone looks.
`solve_diagnostics` is the attribute payload of
`sensor.nimbus_solver_solve_seconds`, and that sensor's history is already how
a #1179 investigation counts cycles.

The recorder genuinely retains those attributes, checked rather than assumed:
a 20-minute window read back from **2026-09-22 21:00 AEST** returns **70 rows,
70 of them carrying the full `solve_diagnostics` key set** (and stamped
`nimbus_version 0.94.413`, that day's release). Five days and several restarts
later. So a base rate published here is still readable long after the log that
would have carried it is gone.

**Observability only.** Nothing branches on any new field; no solve changes
shape. The calibration tolerance and the minimum weight are untouched,
which #1179 lists under "Not proposed" because they re-price live dispatch.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import pathlib
import sys
import unittest
from datetime import UTC, datetime
from unittest import mock

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod
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


class TestInfeasiblePlanPropagatesThem(unittest.TestCase):
    """Gap 2, the hop a failing cycle actually takes."""

    def test_both_reach_the_plan(self):
        plan = net_mod._infeasible_plan(
            _periods(),
            "error",
            iterations=7,
            raw_status="Unknown",
            highs_info={"simplex_iteration_count": 0.0},
            failing_model_path="/tmp/x.mps",
        )
        self.assertEqual(plan.highs_info, {"simplex_iteration_count": 0.0})
        self.assertEqual(plan.failing_model_path, "/tmp/x.mps")
        self.assertEqual(plan.iterations, 7)

    def test_the_dict_is_not_aliased_to_the_callers(self):
        """Same reasoning as #356's `np.zeros(n)`-per-field fix: a caller
        mutating its own dict afterwards must not reach into a frozen Plan."""
        caller = {"simplex_iteration_count": 0.0}
        plan = net_mod._infeasible_plan(
            _periods(), "error", iterations=0, highs_info=caller
        )
        caller["simplex_iteration_count"] = 999.0
        self.assertEqual(plan.highs_info, {"simplex_iteration_count": 0.0})

    def test_omitted_gives_an_empty_dict_not_none(self):
        plan = net_mod._infeasible_plan(_periods(), "error", iterations=0)
        self.assertEqual(plan.highs_info, {})
        self.assertIsNone(plan.failing_model_path)


class TestARealFailedSolveCapturesIt(unittest.TestCase):
    """Gap 2, end to end against real HiGHS -- no mock of the solver."""

    def test_a_terminal_non_optimal_solve_populates_highs_info(self):
        p = _forced_time_limit_problem()
        with mock.patch.object(lp_mod, "DEFAULT_TIME_LIMIT_SECONDS", 0.0):
            r = p.solve()
        self.assertEqual(r.status, "error")
        self.assertTrue(r.raw_status, "raw_status must still name HiGHS's status")
        self.assertIn(
            "simplex_iteration_count",
            r.highs_info,
            "the field lp.py's own comment calls the discriminating one",
        )
        # Sanity on the rest of the snapshot rather than exact values, which
        # are HiGHS-version dependent.
        self.assertIn("num_primal_infeasibilities", r.highs_info)
        self.assertTrue(all(isinstance(v, float) for v in r.highs_info.values()))

    def test_an_optimal_solve_captures_nothing(self):
        """The control. If a healthy solve also populated this, a populated
        dict would say nothing about a failure."""
        r = _forced_time_limit_problem().solve()
        self.assertEqual(r.status, "optimal")
        self.assertEqual(r.highs_info, {})
        self.assertIsNone(r.failing_model_path)

    def test_the_failing_model_is_written_and_named_for_this_issue(self):
        p = _forced_time_limit_problem()
        # `_dump_failing_model()` is once-per-label-per-process, so a test
        # that ran after another terminal failure of the same status would
        # get None. Cleared so this asserts the capture, not the ordering.
        lp_mod._LP_DUMPED_PHASES.clear()
        try:
            with mock.patch.object(lp_mod, "DEFAULT_TIME_LIMIT_SECONDS", 0.0):
                r = p.solve()
        finally:
            lp_mod._LP_DUMPED_PHASES.clear()
        self.assertIsNotNone(
            r.failing_model_path, "the real instance must be captured, per #773"
        )
        assert r.failing_model_path is not None
        name = pathlib.Path(r.failing_model_path).name
        self.assertTrue(
            name.startswith("nimbus_1179_"),
            f"a #1179 capture must not be filed under another issue: {name}",
        )
        self.assertTrue(pathlib.Path(r.failing_model_path).exists())

    def test_the_dump_is_once_per_status_not_once_per_cycle(self):
        """153 failures in 2.7h must not write 153 MPS files."""
        p = _forced_time_limit_problem()
        lp_mod._LP_DUMPED_PHASES.clear()
        try:
            with mock.patch.object(lp_mod, "DEFAULT_TIME_LIMIT_SECONDS", 0.0):
                first = p.solve()
                second = p.solve()
        finally:
            lp_mod._LP_DUMPED_PHASES.clear()
        self.assertIsNotNone(first.failing_model_path)
        self.assertIsNone(second.failing_model_path)
        # ...and the second failure still carries every NUMBER, which is the
        # part that must be per-cycle.
        self.assertIn("simplex_iteration_count", second.highs_info)


class TestTheSnapshotNeverBreaksTheSolve(unittest.TestCase):
    """A diagnostic on an already-failing path must not be the reason a
    cycle dies -- the discipline `_dump_failing_model()` already states."""

    def test_a_handle_with_no_getinfo_returns_empty(self):
        class Broken:
            def getInfo(self):
                raise RuntimeError("no")

        self.assertEqual(lp_mod._highs_info_snapshot(Broken()), {})

    def test_a_missing_field_is_absent_rather_than_zero(self):
        """A zero would read as a measurement."""

        class PartialInfo:
            simplex_iteration_count = 12

        class Partial:
            def getInfo(self):
                return PartialInfo()

        snap = lp_mod._highs_info_snapshot(Partial())
        self.assertEqual(snap, {"simplex_iteration_count": 12.0})
        self.assertNotIn("mip_gap", snap)

    def test_a_non_numeric_field_is_skipped_not_crashed(self):
        class WeirdInfo:
            simplex_iteration_count = 3
            mip_gap = "not a number"

        class Weird:
            def getInfo(self):
                return WeirdInfo()

        self.assertEqual(
            lp_mod._highs_info_snapshot(Weird()), {"simplex_iteration_count": 3.0}
        )


class TestTheFormatter(unittest.TestCase):
    def test_empty_says_unavailable_never_an_empty_string(self):
        """A log line that silently loses its tail cannot be told apart from
        one whose tail was empty -- #757's own lesson about a check that
        cannot fail."""
        self.assertEqual(lp_mod.format_highs_info({}), "unavailable")

    def test_integral_values_print_without_a_decimal_point(self):
        out = lp_mod.format_highs_info({"simplex_iteration_count": 0.0})
        self.assertEqual(out, "simplex_iterations=0")

    def test_infinity_does_not_raise(self):
        """HiGHS returns `mip_gap = inf` on a pure LP, and `int(inf)` raises
        OverflowError. Found by running the real solver, not by reading the
        code: the first draft of the formatter crashed on the very first
        real snapshot it was given."""
        self.assertTrue(math.isinf(float("inf")))
        out = lp_mod.format_highs_info(
            {"simplex_iteration_count": 4.0, "mip_gap": float("inf")}
        )
        self.assertIn("simplex_iterations=4", out)
        self.assertIn("mip_gap=inf", out)

    def test_an_unknown_key_is_still_printed(self):
        """Otherwise the formatter becomes the reason a future field is
        invisible."""
        out = lp_mod.format_highs_info({"something_new": 2.5})
        self.assertIn("something_new=2.5", out)

    def test_the_discriminating_field_is_first(self):
        out = lp_mod.format_highs_info(
            {"mip_node_count": 1.0, "simplex_iteration_count": 0.0}
        )
        self.assertTrue(out.startswith("simplex_iterations=0"), out)


def _publish_plan_node() -> ast.FunctionDef:
    tree = ast.parse((_SRC_DIR / "solver_writer.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "publish_plan":
            return node
    raise AssertionError("publish_plan() not found in solver_writer.py")


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

    Parsed rather than imported: the same approach
    `test_solve_diagnostics_load_counts.py` already uses for this payload,
    because reaching it at runtime means driving the whole publish path.
    """
    tree = ast.parse((_SRC_DIR / "solver_writer.py").read_text(encoding="utf-8"))
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

    def test_the_key_name_matches_the_one_the_failure_line_prints(self):
        """Read side by side, so no translation step stands between the
        failing value and its baseline."""
        self.assertIn("simplex_iterations=", str(_failure_warning_call().args[0].value))
        self.assertIn("simplex_iterations", self.keys)

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

    def test_the_only_branch_anywhere_is_the_log_guard(self):
        """solver_writer.py legitimately tests `failing_model_path` once, to
        decide whether to print a path it does not have. Exactly once."""
        tests = self._tests_of_every_conditional(_SRC_DIR / "solver_writer.py")
        hits = [t for t in tests if any(f in t for f in self._FIELDS)]
        self.assertEqual(
            hits,
            ["plan.failing_model_path is not None"],
            f"unexpected branching on a #1179 diagnostic: {hits}",
        )


if __name__ == "__main__":
    unittest.main()
