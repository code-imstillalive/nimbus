"""A test module that installs a global stub must not be imported by bare name.

## The defect (nimbus issue #1329, root-caused by Mark Purcell)

    $ pytest tests/test_solver_writer_controllable_loads.py \\
             tests/test_commanded_state_guard_reports_its_own_failure.py -q
    35 failed, 90 passed
    $ pytest tests/test_commanded_state_guard_reports_its_own_failure.py \\
             tests/test_solver_writer_controllable_loads.py -q
    125 passed

Reproduced here exactly, on Windows, with `-p no:homeassistant`: **35 failed / 90
passed** in the first order against **125 passed** with the fix. So the suite only
passed in the collection order CI happens to use, and nothing declared that.

`pyproject.toml` sets `pythonpath = ["custom_components/nimbus_load", "tests"]`,
so `tests/` is importable two ways. pytest imports a test file as
`tests.test_x`; a sibling's bare `import test_x` creates a **second module
object for the same file**, and both sit in `sys.modules`:

    ('tests.test_solver_writer_controllable_loads', 140079382933600)
    ('test_solver_writer_controllable_loads',       140079241716304)

That file's module-level code installs its own `_FakeRunStateStore` as
`sys.modules["homeassistant.helpers.storage"]`. The second import runs it again,
and the last import wins — so the installed `Store` belongs to the *second* copy,
while the tests clear `_FakeRunStateStore._shared_data` on the *first* copy's
class. A different class, a different dict: run state carries between tests,
`result.commanded_state` stays False, and `build_controllable_loads` returns a
load where `[]` is expected.

Collection imports every file before any test runs and the last import wins,
which is why the other order passes: the guard file's bare-name copy installs
first and pytest's copy replaces it, so the controllable tests then clear the one
that is installed.

## The invariant, and why it is narrow

**The hazard needs BOTH halves:** a module that writes into `sys.modules` at
import time, AND a sibling importing it by bare name so a second copy exists. A
double import of a module that merely defines helpers is wasteful and harmless;
and a stub installer pytest imports once, under its own `tests.` name, is fine.

Measured: **11** test modules install a stub at import time, and exactly **one**
of them is imported by a sibling — the one above.

That matters, because the first attempt at this fix qualified *all nine* sibling
imports on the reasoning that each was "the same hazard". Eight were not, and one
of them broke something real: `test_changelog_guard_prose_defeat.py` is pure
stdlib, imports no path helper, and **ran standalone** — a mode
`tests/_solver_path.py`'s own docstring promises ("runnable via `python -m
unittest discover tests` or directly via `python tests/test_X.py`"). Qualifying
its import made `python tests/test_changelog_guard_prose_defeat.py` fail with
`ModuleNotFoundError: No module named 'tests'`. Verified before and after: it
passed on `main`, failed with the blanket change, passes again reverted.

So this checks the property that actually bites, not the syntax that resembles
it.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _installs_a_global_stub(path: Path) -> bool:
    """Does this module write into `sys.modules` at import time?

    Module level only. A write inside a function body runs when something calls
    it, which a second import does not do by itself.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a parse error is its own failure
        return False
    for node in tree.body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "modules"
                ):
                    return True
    return False


def _bare_sibling_imports(path: Path) -> set[str]:
    """Sibling test modules this file imports WITHOUT the `tests.` prefix."""
    out: set[str] = set()
    src = path.read_text(encoding="utf-8")
    for m in re.finditer(
        r"^\s*(?:from|import)\s+(test_[A-Za-z0-9_]+)", src, re.MULTILINE
    ):
        out.add(m.group(1))
    return out


def _qualified_sibling_imports(path: Path) -> set[str]:
    """Sibling test modules imported WITH the `tests.` prefix -- the fixed form."""
    out: set[str] = set()
    src = path.read_text(encoding="utf-8")
    for m in re.finditer(
        r"^\s*(?:from|import)\s+tests\.(test_[A-Za-z0-9_]+)", src, re.MULTILINE
    ):
        out.add(m.group(1))
    return out


class TestNoBareImportOfAStubInstallingModule(unittest.TestCase):
    def test_the_hazardous_combination_does_not_occur(self):
        hazardous = {
            p.stem for p in _TESTS.glob("test_*.py") if _installs_a_global_stub(p)
        }
        offenders = []
        for path in sorted(_TESTS.glob("test_*.py")):
            for sibling in _bare_sibling_imports(path) & hazardous:
                offenders.append(f"{path.name} -> {sibling}")
        self.assertEqual(
            offenders,
            [],
            "a test module that installs a global stub via sys.modules is being "
            "imported by BARE name, which creates a second module object for the "
            "same file and makes the installed stub a different class from the one "
            "tests reset -- 35 order-dependent failures (nimbus #1329). Import it "
            "as `tests.<name>` instead: " + ", ".join(offenders),
        )

    def test_the_known_stub_installer_is_still_detected(self):
        """Guard against the guard passing vacuously.

        If `_FakeRunStateStore` ever moves behind a helper call, the hazardous
        set goes empty and the real check above passes while examining nothing.
        """
        target = _TESTS / "test_solver_writer_controllable_loads.py"
        self.assertTrue(
            _installs_a_global_stub(target),
            f"{target.name} installs a stub into sys.modules at module level; if "
            "that stopped being true, re-derive this guard's premise",
        )

    def test_the_narrowness_is_real_not_assumed(self):
        """Of the modules a sibling imports, exactly one installs a stub.

        Stated over the IMPORTED set, not over every test file. Many test
        modules install stubs at import time -- measured, 11 of them -- and that
        is fine, because pytest imports each of them once, under its own
        `tests.` name. The hazard needs both halves: a stub installer AND a
        sibling importing it by bare name, creating the second copy.

        Getting this wrong is what produced the over-broad first fix. It is
        asserted here so the narrow scope is a measured claim rather than a
        remembered one, and so that a second sibling-imported stub installer
        fails loudly -- at which point Mark's own longer-term suggestion on
        #1329 (move shared fakes into a helper nothing collects) becomes the
        better answer than qualifying imports one at a time.
        """
        imported: set[str] = set()
        for path in _TESTS.glob("test_*.py"):
            imported |= _bare_sibling_imports(path)
            imported |= _qualified_sibling_imports(path)
        hazardous = sorted(
            name
            for name in imported
            if (_TESTS / f"{name}.py").exists()
            and _installs_a_global_stub(_TESTS / f"{name}.py")
        )
        self.assertEqual(
            hazardous,
            ["test_solver_writer_controllable_loads"],
            "the set of sibling-imported stub-installing modules changed; each "
            "new one is a fresh instance of #1329",
        )


class TestStandaloneExecutionStillWorks(unittest.TestCase):
    """The mode the blanket fix broke, pinned so it is not broken again.

    `tests/_solver_path.py`'s docstring promises `python tests/test_X.py` works.
    The qualified import needs the repo root on `sys.path` for that, which is why
    `_solver_path` adds it.
    """

    def test_solver_path_puts_the_repo_root_on_sys_path(self):
        import _solver_path  # noqa: F401

        root = str(_TESTS.parent)
        self.assertIn(
            root,
            sys.path,
            "_solver_path must add the repo root so `import tests.test_x` "
            "resolves under standalone execution, not only under pytest",
        )

    def test_the_repo_root_is_appended_not_inserted(self):
        """Same reasoning `_solver_path` already documents for the integration
        directory: nothing on this path may shadow a stdlib module. Appending
        keeps stdlib winning."""
        src = (_TESTS / "_solver_path.py").read_text(encoding="utf-8")
        self.assertIn("sys.path.append(_REPO_ROOT)", src)
        self.assertNotIn("sys.path.insert(0, _REPO_ROOT)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
