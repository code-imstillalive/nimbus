"""Pins nimbus issue #1301's Phase 2b extraction: the three independent
reporting subsystems moved out of `solver_writer.py` into `solver_reports/`.

What these tests are actually for. The move itself was verified
MECHANICALLY rather than behaviourally -- every inserted `sw.` was
stripped back off and the result compared byte-for-byte against the
original lines of `solver_writer.py`, so the relocated code is provably
the same code (#1298's methodology item 3, the technique #1019 used for
its re-indent). That check happens once, at extraction time, and cannot be
re-run afterwards.

What CAN regress afterwards is the seam -- the reason the moved code says
`sw.helper(...)` rather than importing `helper`. These tests exist so a
future "cleanup" of that pattern fails here, loudly, with the reason
attached, instead of silently converting 12 test files' worth of
`patch.object(solver_writer, "fetch_entity_history_range", ...)` into
calls against the real helper. For the I/O helpers that means a unit test
attempting a live HTTP request.

See `solver_reports/__init__.py` for the full reasoning and the measured
patch counts, and `solver_inputs/__init__.py` for the original write-up of
the same seam.

stdlib `unittest` only, and `subTest` rather than a parametrize decorator,
so this module keeps working under `python -m unittest discover tests` --
the mode `_solver_path`'s own docstring promises and 306 of this
directory's test modules honour.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401 -- sys.path setup side effect
import solver_writer
from solver_reports import backtest, counterfactual, flex

_PACKAGE_DIR = pathlib.Path(solver_writer.__file__).parent / "solver_reports"

# (facade attribute, module it moved to) for every name Phase 2b relocated.
MOVED = (
    ("compute_efficiency_backtest_report", backtest),
    ("compute_nimbus_only_soc_counterfactual", counterfactual),
    ("_compute_flex_report_for_window", flex),
    ("FLEX_SIGNALS_ENTITY_ID", flex),
    ("_PRICE_BAND_WIDTH", flex),
)
_MODULES = (("backtest", backtest), ("counterfactual", counterfactual), ("flex", flex))


class TestTheFacadeStillAnswersForEveryMovedName(unittest.TestCase):
    """`solver_writer.X` must BE the moved object, not a copy of it.

    153 test files and 4 production modules import `solver_writer`
    directly. #1298 made the facade non-negotiable for that reason: the
    extraction is only pure if every one of those call sites keeps
    resolving the identical object.
    """

    def test_facade_attribute_is_the_moved_object(self):
        for name, module in MOVED:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(solver_writer, name),
                    getattr(module, name),
                    f"solver_writer.{name} is no longer the same object as "
                    f"{module.__name__}.{name} -- the re-export alias has "
                    f"drifted, so call sites and tests that resolve it through "
                    f"solver_writer now exercise something else.",
                )


class TestDeferredImportSeamIsLoadBearing(unittest.TestCase):
    """A patch on the `solver_writer` module object must reach moved code.

    This is precisely the property a module-scope `from ..solver_writer
    import helper` destroys, and the whole reason for the `sw.` idiom.
    Measured when Phase 2b landed: `fetch_entity_history_range` is patched
    this way in 12 test files, `_LOGGER` in 6, `_kw_scale_factor` in 1.
    """

    def test_patching_solver_writer_is_visible_through_the_accessor(self):
        for tag, module in _MODULES:
            with self.subTest(module=tag):
                sentinel = object()
                with patch.object(
                    solver_writer, "fetch_entity_history_range", return_value=sentinel
                ):
                    got = module._solver_writer().fetch_entity_history_range(
                        "sensor.x", None, None
                    )
                self.assertIs(
                    got,
                    sentinel,
                    f"{module.__name__} resolved fetch_entity_history_range at "
                    f"IMPORT time instead of call time -- "
                    f"patch.object(solver_writer, ...) no longer reaches it.",
                )

    def test_accessor_returns_the_real_module_object(self):
        for tag, module in _MODULES:
            with self.subTest(module=tag):
                self.assertIs(module._solver_writer(), solver_writer)


class TestNoModuleScopeImportFromSolverWriter(unittest.TestCase):
    """The anti-pattern guard, checked against the source.

    A module-scope `from ..solver_writer import x` binds `x` at import
    time. The runtime test above would keep passing for the one helper it
    names while a newly-imported one silently escaped, so this checks the
    shape of every file in the package directly.
    """

    def test_no_top_level_import_from_solver_writer(self):
        paths = sorted(_PACKAGE_DIR.glob("*.py"))
        self.assertTrue(paths, f"no modules found in {_PACKAGE_DIR}")
        for path in paths:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                offenders = [
                    f"line {n.lineno}: from {n.module} import "
                    + ", ".join(a.name for a in n.names)
                    for n in tree.body
                    if isinstance(n, ast.ImportFrom)
                    and (n.module or "").endswith("solver_writer")
                ]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path.name} imports from solver_writer at MODULE scope: "
                    + "; ".join(offenders)
                    + ". Use the deferred `sw = _solver_writer()` accessor "
                    "instead -- see solver_reports/__init__.py.",
                )


class TestMovedFunctionsBindTheAccessor(unittest.TestCase):
    """Every moved function must actually bind `sw` before using it.

    Not hypothetical. The first cut of this extraction qualified all the
    names as `sw.<name>` and omitted the `sw = _solver_writer()` line,
    which surfaced as 24 test failures and ~92 ruff F821 findings. Ruff
    would catch a bare `sw` again; this states the requirement where
    someone reading the package will actually see it.
    """

    def test_function_binds_sw_from_the_accessor(self):
        cases = (
            (backtest, "compute_efficiency_backtest_report"),
            (counterfactual, "compute_nimbus_only_soc_counterfactual"),
            (flex, "_compute_flex_report_for_window"),
        )
        for module, fname in cases:
            with self.subTest(function=fname):
                tree = ast.parse(
                    pathlib.Path(module.__file__).read_text(encoding="utf-8")
                )
                fn = next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == fname
                )
                binds = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "sw" for t in n.targets
                    )
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name)
                    and n.value.func.id == "_solver_writer"
                ]
                self.assertTrue(
                    binds, f"{fname}() never binds sw = _solver_writer()"
                )


if __name__ == "__main__":
    unittest.main()
