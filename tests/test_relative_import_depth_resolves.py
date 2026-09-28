"""Every relative import in the integration must resolve to something that exists.

## The bug this exists to catch, found on devhub rather than by this suite

v0.94.428 shipped with `solver_inputs/controllable_loads.py` containing **seven**
relative imports written at the wrong depth -- `from . import done_condition`,
`from .const import ...`, `from .solver import elements` -- where
`done_condition.py`, `const.py` and `solver/` all live one level UP, in
`custom_components/nimbus_load/`, not inside `solver_inputs/`.

Phase 3 of #1298 (#1302) moved `build_controllable_loads` and its seven helpers out
of `solver_writer.py`, which sits at the package ROOT, into `solver_inputs/`. At the
root `from . import done_condition` was correct. One level down it is not, and the
depth was not adjusted. The module-level import block at the top of that same file
*was* written correctly with `..`, which is what makes the inconsistency easy to
miss by reading.

The live symptom on devhub, first solve after the v0.94.428 restart:

    File ".../solver_inputs/controllable_loads.py", line 627
        from . import done_condition, load_run_state, thermal_forecast
    ImportError: cannot import name 'done_condition' from
        'custom_components.nimbus_load.solver_inputs'
    ...
    ModuleNotFoundError: No module named 'done_condition'

`Nimbus Solver: solve cycle failed`, 16 times. **Every native-mode solve.**

## Why the whole 4,500-test suite passed anyway

Each of those imports sits in a `try: ... except ImportError:` pair whose fallback
is a bare absolute import (`import done_condition`), there for the standalone/cron
deployment which has no parent package. `pyproject.toml` sets
`pythonpath = ["custom_components/nimbus_load", "tests"]`, so under pytest the bare
absolute import **succeeds** -- the `except` branch quietly does the right thing and
the broken `try` branch is never visible. Under real HA there is no such sys.path
entry, so the fallback raises `ModuleNotFoundError` and the solve dies.

That is why this check is deliberately **static**. Importing the modules to test
them would reproduce the same masking, because the test process is the one with the
convenient sys.path. Reading the source and resolving each relative import against
the real directory tree does not depend on how the process was launched.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_NIMBUS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)


def _resolve(module_path: pathlib.Path, level: int, module: str | None) -> pathlib.Path:
    """The directory a relative import's `level` dots point at.

    `level=1` is the package containing this module, `level=2` its parent, and so
    on -- exactly Python's own rule.
    """
    base = module_path.parent
    for _ in range(level - 1):
        base = base.parent
    return base if module is None else base / module.replace(".", "/")


def _exists(target: pathlib.Path) -> bool:
    return (
        target.is_dir()
        and (target / "__init__.py").exists()
        or target.with_suffix(".py").exists()
    )


def _names_in_init(package_dir: pathlib.Path) -> frozenset[str]:
    """Top-level names a package's own `__init__.py` defines.

    `from . import X` is valid when X is a submodule OR a name defined in the
    package's `__init__.py` -- `number.py` and `switch.py` both do the latter with
    `_configure_price_watcher`, and `flows/signal_subentry.py` with
    `find_subentry_sharing_source_sensor`. Without this the check reports three
    false positives and would have to be weakened to stay green, which is how a
    guard stops guarding.
    """
    init = package_dir / "__init__.py"
    if not init.exists():
        return frozenset()
    names: set[str] = set()
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return frozenset(names)


class TestRelativeImportDepthResolves(unittest.TestCase):
    def test_every_relative_import_points_at_something_real(self):
        """A relative import whose target does not exist on disk is a runtime
        failure in the real package, however green this suite is."""
        checked = 0
        broken: list[str] = []
        for path in sorted(_NIMBUS_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                base = _resolve(path, node.level, node.module)
                if node.module is None:
                    # `from . import a, b` -- each name is a submodule OR a name
                    # the target package's own __init__.py defines
                    init_names = _names_in_init(base)
                    for alias in node.names:
                        checked += 1
                        if (
                            not _exists(base / alias.name)
                            and alias.name not in init_names
                        ):
                            rel = (base / alias.name).relative_to(_NIMBUS_DIR.parent)
                            broken.append(
                                f"{path.relative_to(_NIMBUS_DIR)}:{node.lineno}  "
                                f"from {'.' * node.level} import {alias.name}"
                                f"  ->  {rel}"
                            )
                else:
                    checked += 1
                    # `from .pkg import NAME` -- resolve the MODULE only; the
                    # imported names may be attributes rather than submodules.
                    if not _exists(base):
                        broken.append(
                            f"{path.relative_to(_NIMBUS_DIR)}:{node.lineno}  "
                            f"from {'.' * node.level}{node.module} import ..."
                            f"  ->  {base.relative_to(_NIMBUS_DIR.parent)}"
                        )
        self.assertGreater(checked, 100, "non-vacuity: too few relative imports found")
        self.assertEqual(
            broken,
            [],
            "relative imports whose target does not exist. Under pytest these are "
            "masked by pyproject's `pythonpath` making the `except ImportError` "
            "absolute fallback succeed; under real Home Assistant they raise and "
            "kill the solve. This is nimbus #1302's own regression, shipped in "
            "v0.94.428 and found on devhub:\n  " + "\n  ".join(broken),
        )

    def test_the_check_would_have_caught_the_v0_94_428_regression(self):
        """Non-vacuity, and a guard against the resolver silently accepting
        anything. Synthesises the exact shape that shipped -- a module inside a
        subpackage importing a root-level module with one dot -- and requires it
        to be rejected, and the two-dot form to be accepted."""
        inner = _NIMBUS_DIR / "solver_inputs" / "controllable_loads.py"
        self.assertTrue(inner.exists(), "fixture module moved")

        # one dot from inside solver_inputs/ -> solver_inputs/done_condition.py
        one_dot = _resolve(inner, 1, None) / "done_condition"
        self.assertFalse(
            _exists(one_dot),
            "`from . import done_condition` must NOT resolve from inside "
            "solver_inputs/ -- if this passes, done_condition.py has been added "
            "there and this test's premise is stale",
        )

        # two dots -> nimbus_load/done_condition.py, which is where it really is
        two_dot = _resolve(inner, 2, None) / "done_condition"
        self.assertTrue(
            _exists(two_dot),
            "`from .. import done_condition` must resolve from inside solver_inputs/",
        )

    def test_controllable_loads_uses_the_right_depth_everywhere(self):
        """The specific file that shipped broken, pinned by name.

        Its module-level block was already correct with `..` while seven
        function-level imports used `.`, so a file-wide "does it use `..`
        anywhere" check would have passed. This asserts every single-dot import in
        it names a real sibling."""
        path = _NIMBUS_DIR / "solver_inputs" / "controllable_loads.py"
        siblings = {p.stem for p in path.parent.glob("*.py")} - {"__init__"}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            names = (
                [a.name for a in node.names]
                if node.module is None
                else [node.module.split(".")[0]]
            )
            init_names = _names_in_init(path.parent)
            for name in names:
                if name not in siblings and name not in init_names:
                    offenders.append(f"line {node.lineno}: {name}")
        self.assertEqual(
            offenders,
            [],
            "single-dot imports in controllable_loads.py that are not "
            f"solver_inputs/ siblings: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
