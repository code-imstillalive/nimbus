"""nimbus issue #1302 (spec 003) -- solver_inputs/controllable_loads.py.

Phase 3 moves eight functions out of `solver_writer.py` into
`solver_inputs/controllable_loads.py`, verbatim, and `safe_num` into
`solver_shared.py`. Every one of the nine keeps a re-export in
`solver_writer.py`, so every existing caller and every
`patch.object(solver_writer, ...)` site keeps resolving the same object.

This is `tests/test_solver_reports_extraction.py` applied to Phase 3's own
target, for the same reasons that file states at length -- and scoped to the
WHOLE `solver_inputs/` package rather than only the new module, because the
four modules #1363 and Phases 1-2 already put there were never covered by
those structural checks and are subject to exactly the same three traps
(`global` in relocated code, a deferred accessor in a default argument, a
module-scope import from `solver_writer`).

## What Phase 3 cost that spec 003's own caller analysis did not predict

Both findings are recorded here because each is now a test:

- **A facade fixes name resolution for CALLERS. It does nothing for the
  moved function's OWN dependency lookups.** `safe_num` calls `ha_get`, and
  once it lives in `solver_shared.py` it resolves `ha_get` from THAT
  module's namespace -- so nine test files patching only
  `solver_writer.ha_get` let a real HTTP call escape. Spec 003's caller
  table lists `safe_num` as having 0 monkeypatch sites, which is true of
  `safe_num` itself and irrelevant to what it reads.
- **`_sample_load_run_state` had a monkeypatch site the table records as
  zero.** `tests/test_solver_writer_thermal_load_resolution.py` replaced
  `solver_writer._sample_load_run_state` and then called
  `build_controllable_loads`. Once both live in this module, the callee
  reads the bare intra-module name, so patching the facade became a silent
  no-op -- the #1316 failure mode `tests/gates/noop_patches.py` exists to
  catch, which surfaced here as a real assertion failure only because that
  test checks a learned value the module default does not produce.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401 -- sys.path setup side effect
import solver_shared
import solver_writer
from solver_inputs import controllable_loads

_PACKAGE_DIR = pathlib.Path(solver_writer.__file__).parent / "solver_inputs"

# (facade attribute, module it moved to) for every name spec 003 relocated.
MOVED = (
    ("_resolve_hour_to_period_index", controllable_loads),
    ("_earliest_period_for_same_day_window", controllable_loads),
    ("_build_daily_adequacy_windows", controllable_loads),
    ("_evaluate_done_condition", controllable_loads),
    ("_resolve_controllable_load_tuning", controllable_loads),
    ("_sample_load_run_state", controllable_loads),
    ("resolve_controllable_load_power_sensor", controllable_loads),
    ("build_controllable_loads", controllable_loads),
    ("safe_num", solver_shared),
)

# Every module in the package, so the three structural rules below cover the
# four that predate this spec as well as the one it adds.
_MODULES = tuple(
    (p.stem, p) for p in sorted(_PACKAGE_DIR.glob("*.py")) if p.name != "__init__.py"
)


class TestTheFacadeStillAnswersForEveryMovedName(unittest.TestCase):
    """`solver_writer.X` must BE the moved object, not a copy of it."""

    def test_facade_attribute_is_the_moved_object(self):
        for name, module in MOVED:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(solver_writer, name),
                    getattr(module, name),
                    f"solver_writer.{name} is no longer the same object as "
                    f"{module.__name__}.{name} -- the re-export alias has "
                    f"drifted, so call sites and tests that resolve it "
                    f"through solver_writer now exercise something else.",
                )

    def test_parse_done_when_alias_survived_losing_its_last_caller(self):
        """`solver_writer._parse_done_when` is read by no production code in
        that file any more -- the eight bodies that used it moved here. It is
        kept anyway, because spec 001's identity invariant is that every name
        in scope before a relocation still resolves after it, and
        `tests/test_solver_writer_controllable_loads.py::TestParseDoneWhen`
        reads it. Deleting it as an unused import (which `ruff --fix` does
        offer to do, and did once) breaks six tests."""
        import done_condition

        self.assertIs(solver_writer._parse_done_when, done_condition.parse_done_when)


class TestDeferredImportSeamIsLoadBearing(unittest.TestCase):
    """A patch on the `solver_writer` module object must reach moved code.

    Measured for this module specifically: 60 test files patch
    `solver_writer.ha_get`, and `resolve_controllable_load_power_sensor` --
    which moves here and is still reached as `sw.<name>` by
    `build_controllable_loads` -- is patched on `solver_writer` by
    `tests/test_768_controllable_load_delivery_reconstruction.py`.
    """

    def test_patching_solver_writer_is_visible_through_the_accessor(self):
        sentinel = object()
        with patch.object(solver_writer, "ha_get", return_value=sentinel):
            got = controllable_loads._solver_writer().ha_get("sensor.x")
        self.assertIs(
            got,
            sentinel,
            "controllable_loads resolved ha_get at IMPORT time instead of "
            "call time -- patch.object(solver_writer, ...) no longer reaches "
            "it.",
        )

    def test_accessor_returns_the_real_module_object(self):
        self.assertIs(controllable_loads._solver_writer(), solver_writer)

    def test_the_power_sensor_resolver_is_reached_through_the_accessor(self):
        """Not `is`-identity but the shape that makes the #768 monkeypatch
        work: `build_controllable_loads` must call this through `sw.`, even
        though the function now lives beside it. A bare intra-module call
        would make that test's spy invisible -- the Phase 2c
        `compute_quality_report` failure, which cost four tests."""
        src = pathlib.Path(controllable_loads.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "build_controllable_loads"
        )
        reached = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and n.attr == "resolve_controllable_load_power_sensor"
            and isinstance(n.value, ast.Name)
            and n.value.id == "sw"
        ]
        self.assertTrue(
            reached,
            "build_controllable_loads() no longer calls "
            "sw.resolve_controllable_load_power_sensor(...) -- "
            "test_768_controllable_load_delivery_reconstruction.py patches "
            "that name on solver_writer, and a bare call here makes the "
            "patch a silent no-op.",
        )


class TestLoggerIsReachedThroughSolverShared(unittest.TestCase):
    """Never `sw._LOGGER`, in any module of this package.

    `tests/test_callers_mode_counts_only_real_references.py` asserts zero
    real `sw._LOGGER`-shaped references outside `solver_writer.py` -- Phase
    2a's own success condition. Stated here too because a regeneration of
    these bodies from a shifted `solver_writer.py` reintroduces the bare
    `_LOGGER` form, and a naive qualifying pass turns it into exactly the
    shape that gate rejects.
    """

    def test_no_module_reaches_logger_through_the_accessor(self):
        for name, path in _MODULES:
            with self.subTest(module=name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                offenders = [
                    f"line {n.lineno}"
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute)
                    and n.attr == "_LOGGER"
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "sw"
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path.name} reaches _LOGGER as sw._LOGGER at "
                    + ", ".join(offenders)
                    + ". Use solver_shared._LOGGER -- it is the same logger "
                    "object (spec 001 makes solver_writer._LOGGER an identity "
                    "alias of it), and the sw. form fails the callers-mode "
                    "inventory gate.",
                )


class TestNoModuleScopeImportFromSolverWriter(unittest.TestCase):
    """A module-scope `from ..solver_writer import x` binds `x` at import
    time, which is precisely what the `sw.` idiom exists to avoid."""

    def test_no_top_level_import_from_solver_writer(self):
        self.assertTrue(_MODULES, f"no modules found in {_PACKAGE_DIR}")
        for name, path in _MODULES:
            with self.subTest(module=name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                offenders = [
                    f"line {n.lineno}: from {n.module} import "
                    + ", ".join(a.name for a in n.names)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)
                    and (n.module or "").endswith("solver_writer")
                    and n.col_offset == 0
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path.name} imports from solver_writer at MODULE scope: "
                    + "; ".join(offenders)
                    + ". Use the deferred `sw = _solver_writer()` accessor "
                    "instead.",
                )


class TestMovedFunctionsBindTheAccessor(unittest.TestCase):
    """Every moved function that uses `sw` must bind it first.

    Not hypothetical: Phase 2b qualified all the names as `sw.<name>` and
    omitted the `sw = _solver_writer()` line, which surfaced as 24 test
    failures and ~92 ruff F821 findings.
    """

    def test_function_binds_sw_from_the_accessor(self):
        src = pathlib.Path(controllable_loads.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name == "_solver_writer":
                continue
            uses_sw = any(
                isinstance(n, ast.Name) and n.id == "sw" and isinstance(n.ctx, ast.Load)
                for n in ast.walk(fn)
            )
            if not uses_sw:
                continue
            with self.subTest(function=fn.name):
                binds = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "sw" for t in n.targets)
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name)
                    and n.value.func.id == "_solver_writer"
                ]
                self.assertTrue(binds, f"{fn.name}() reads sw but never binds it")


class TestNoGlobalStatementInAMovedModule(unittest.TestCase):
    """A `global` statement in relocated code binds in the WRONG module.

    `global X` binds in the namespace of the module the function is
    *defined* in, so moving the function silently moves the variable it
    writes -- and a facade alias cannot repair it, because an alias captures
    the current object while a rebound name gets replaced. Phase 2c's
    `_LAST_KNOWN_QUALITY_HISTORY` cost 5 failures this way.

    **The hazard is a SHARED name, not a `global` statement.** Spec 002's
    version of this test forbids `global` outright in `solver_reports/`,
    which happens to hold because that package has none. Applied to
    `solver_inputs/` it fails on a legitimate case:
    `battery_soc.py`'s `global _HOME_BATTERY_SOC_EXCURSION_WARNED`, where
    the variable moved out of `solver_writer.py` *together with its only
    reader* and `tests/test_solver_inputs_battery_soc.py` reads it from
    `battery_soc`. Nothing resolves it through `solver_writer` any more, so
    binding it here is exactly right.

    So this checks the property that actually breaks: a `global X` where
    `solver_writer.py` ALSO defines `X` at module level -- two homes for one
    rebound name, with the writer's readers left watching the wrong one.
    """

    def test_no_moved_module_rebinds_a_name_solver_writer_also_owns(self):
        writer_tree = ast.parse(
            pathlib.Path(solver_writer.__file__)
            .with_suffix(".py")
            .read_text(encoding="utf-8")
        )
        writer_globals = {
            t.id
            for n in writer_tree.body
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        } | {
            n.target.id
            for n in writer_tree.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
        }
        for name, path in _MODULES:
            with self.subTest(module=name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                offenders = [
                    f"line {n.lineno}: global {declared}"
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Global)
                    for declared in n.names
                    if declared in writer_globals
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path.name} rebinds a name solver_writer.py also "
                    "defines at module level: " + "; ".join(offenders) + ". "
                    "The `global` binds HERE, so solver_writer's own readers "
                    "would keep watching a variable nothing writes. Either "
                    "reach it as `sw.<name>` or move the definition out of "
                    "solver_writer.py entirely, the way battery_soc.py did.",
                )


class TestNoDefaultArgumentReachesThroughTheAccessor(unittest.TestCase):
    """A default argument must never be `sw.SOMETHING` -- defaults are
    evaluated at function-DEFINITION time, before the accessor has run."""

    def test_no_moved_function_uses_sw_in_a_default_argument(self):
        for name, path in _MODULES:
            with self.subTest(module=name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                offenders = []
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    defaults = list(fn.args.defaults) + [
                        d for d in fn.args.kw_defaults if d is not None
                    ]
                    for d in defaults:
                        for sub in ast.walk(d):
                            if (
                                isinstance(sub, ast.Attribute)
                                and isinstance(sub.value, ast.Name)
                                and sub.value.id == "sw"
                            ):
                                offenders.append(
                                    f"{fn.name}() line {sub.lineno}: sw.{sub.attr}"
                                )
                self.assertEqual(
                    offenders,
                    [],
                    f"{path.name} evaluates the deferred accessor in a "
                    "default argument: " + "; ".join(offenders),
                )


if __name__ == "__main__":
    unittest.main()
