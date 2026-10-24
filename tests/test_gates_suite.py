"""Gates on the test suite itself: tests that drive main() isolate their
state paths, stub-installing modules are imported safely, the golden
scenarios are not vacuous and are determinate, a PR names every file it
deletes, closing keywords are caught in commit messages too, and the GBRT
split search matches its reference (#1837 group D: kept, merged, each shown
to fail once on a planted violation)."""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import textwrap
import zlib
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
import numpy as np
import pytest
from _isolated_state import PERSISTED_PATH_CONSTANTS
from golden import scenarios_thermal as st
from nimbus_load.ml import gbrt

# --- test_1185_closing_keyword_guard_misses_commit_messages -----------------------------------
# IV&V finding (c881b523..6d3a6d0 pass, 2026-09-21): #1158's own PR-body
# closing-keyword guard (`.github/scripts/check_pr_closing_keywords.py`, wired
# into `.github/workflows/ci.yml`'s `pr-body-hygiene` job) only ever receives
# `github.event.pull_request.body`. GitHub's own issue-closing scanner does not
# only read the PR description -- it also reads the commit message that lands
# on the default branch.

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _pr_body_hygiene_job_text() -> str:
    """The pr-body-hygiene job's own block, isolated from the rest of
    the workflow file so a match elsewhere in ci.yml can't hide this
    job's real scope."""
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    start = text.index("pr-body-hygiene:")
    # The job block runs until the next top-level (0-indent) job key.
    rest = text[start + len("pr-body-hygiene:") :]
    lines = rest.splitlines()
    end_offset = len(rest)
    offset = 0
    for line in lines:
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            end_offset = offset
            break
        offset += len(line) + 1
    return text[start : start + len("pr-body-hygiene:") + end_offset]


# Any of these appearing in the job block would indicate commit-message
# content is being fed to the guard alongside the PR body -- none do
# today, which is the finding.
_COMMIT_MESSAGE_SOURCES = (
    "pull_request.commits",
    "git log",
    "git show",
    "COMMIT_MESSAGES",
)


class TestTheGuardCoversCommitMessages(unittest.TestCase):
    def test_the_job_reads_the_pr_body(self):
        """Sanity check that the isolated block is the right one and the
        parsing above is not silently matching nothing."""
        job_text = _pr_body_hygiene_job_text()
        self.assertIn("pull_request.body", job_text)

    # Was xfail(strict=True) when Mark filed this. Closed in the same
    # pass: the job gained a second step running the SAME script over
    # `git log --format=%B BASE..HEAD`, with fetch-depth: 0 so there
    # is history to range over.
    def test_the_job_also_reads_commit_message_content(self):
        job_text = _pr_body_hygiene_job_text()
        found = [needle for needle in _COMMIT_MESSAGE_SOURCES if needle in job_text]
        self.assertTrue(
            found,
            "the pr-body-hygiene job checks only the PR body -- no commit "
            "message source (git log, git show, or the PR's own commit "
            "list) is fed to the closing-keyword guard, so a keyword "
            "narrowed only in an early commit message survives past a "
            "clean final PR body once GitHub's own squash-merge folds "
            "that commit message into the merge commit verbatim",
        )


# --- test_1329_sibling_import_of_a_stub_installing_module -------------------------------------
# A test module that installs a global stub must not be imported by bare name.

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
    # Walked by hand: `ast.walk` would still descend into a def's body.
    pending = list(tree.body)
    while pending:
        sub = pending.pop()
        if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        pending.extend(ast.iter_child_nodes(sub))
        if isinstance(sub, ast.Assign):
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
        # Synthetic modules, so the check survives any one test file being
        # retired (#1837), and covers the function-body case too.
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "test_installer.py"
            installer.write_text(
                "import sys\nsys.modules['x'] = object()\n", encoding="utf-8"
            )
            inside = Path(tmp) / "test_inside.py"
            inside.write_text(
                "import sys\ndef f():\n    sys.modules['x'] = object()\n",
                encoding="utf-8",
            )
            self.assertTrue(_installs_a_global_stub(installer))
            self.assertFalse(_installs_a_global_stub(inside))

    def test_the_narrowness_is_real_not_assumed(self):
        """Of the modules a sibling imports, none installs a stub.

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
        # Empty since #1837 retired test_solver_writer_controllable_loads, the
        # one sibling-imported stub installer this was written for.
        self.assertEqual(
            hazardous,
            [],
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


# --- test_1330_main_driving_tests_isolate_state_paths -----------------------------------------
# A test that drives the real `main()` must not write production state.

_TESTS_1330_main_driving_tests_ = Path(__file__).resolve().parent


#: Files exempt from the isolation requirement, each with its reason. A test
#: whose whole subject IS one of these paths has to name it explicitly; adding
#: to this list is a deliberate act, which is the point of it being a list.
_EXEMPT = {
    # Asserts on the constants themselves via the AST; never runs main().
    "test_1324_deleted_runtime_files_are_not_in_config.py",
    # This file.
    "test_gates_suite.py",
}


#: Drivers whose paths are redirected by the process that launches them, each
#: mapped to that launcher. The golden master runs `main()` in a child process
#: whose environment `harness.py` builds; the check below reads the harness.
_ISOLATED_BY_ENV = {"golden/child.py": "golden/harness.py"}


def _test_files() -> list[Path]:
    """Every Python file under tests/, subfolders included (#1837 moved the
    main()-driving tests into tests/golden)."""
    return sorted(
        p
        for p in _TESTS_1330_main_driving_tests_.rglob("*.py")
        if p.name not in _EXEMPT and "__pycache__" not in p.parts
    )


def _rel(path: Path) -> str:
    return path.relative_to(_TESTS_1330_main_driving_tests_).as_posix()


def _env_var_of_each_persisted_path() -> dict[str, str]:
    """`solver_writer`'s own `CONST = os.environ.get("ENV", ...)` pairs."""
    writer = (
        _TESTS_1330_main_driving_tests_.parent
        / "custom_components"
        / "nimbus_load"
        / "solver_writer.py"
    )
    out = {}
    for node in ast.parse(writer.read_text(encoding="utf-8")).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        args = node.value.args
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id in PERSISTED_PATH_CONSTANTS
                and args
                and isinstance(args[0], ast.Constant)
            ):
                out[target.id] = args[0].value
    return out


def _isolates(tree: ast.AST) -> bool:
    """`isolated_state_paths(...)` actually CALLED, not merely imported.

    Checking for the name as text passes on the import line alone. Proven by
    reintroducing the defect in one file: with the helper call swapped back for
    a hardcoded `patch.object`, a text check still passed because
    `from _isolated_state import isolated_state_paths` was still at the top.
    Only the hardcoded-path check caught it. This closes that hole, so a file
    that imports the helper and forgets to use it fails too.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isolated_state_paths"
        ):
            return True
    return False


def _calls_main(tree: ast.AST) -> bool:
    """`solver_writer.main()` as a real call, not a mention in prose.

    A text search would match this module's own docstring, and it matched two
    files whose only reference to `main()` is a comment explaining where a
    function used to live — which is how the original count of eleven files came
    out two too high.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "main"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "solver_writer"
        ):
            return True
    return False


class TestEveryMainDrivingTestIsolatesItsStatePaths(unittest.TestCase):
    def test_no_main_driving_test_omits_isolation(self):
        offenders = []
        for path in _test_files():
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except (
                SyntaxError
            ) as exc:  # pragma: no cover - a parse error is its own failure
                self.fail(f"{path.name} does not parse: {exc}")
            if not _calls_main(tree) or _rel(path) in _ISOLATED_BY_ENV:
                continue
            if not _isolates(tree):
                offenders.append(_rel(path))
        self.assertEqual(
            offenders,
            [],
            "these drive the real solver_writer.main(), whose persisted paths "
            "default to real /opt production files, without redirecting them "
            "(nimbus #1330). Add `isolated_state_paths(solver_writer)` to the "
            "with-block -- see tests/_isolated_state.py: " + ", ".join(offenders),
        )

    def test_at_least_one_file_is_actually_checked(self):
        """Guard against the guard passing vacuously.

        If `_calls_main` ever stopped matching -- a rename, a refactor behind a
        helper -- the loop above would examine nothing and pass.
        """
        driving = [
            _rel(p)
            for p in _test_files()
            if _calls_main(ast.parse(p.read_text(encoding="utf-8")))
        ]
        # Since #1837 the golden master is the main()-driving test.
        self.assertIn("golden/child.py", driving)

    def test_a_launcher_redirects_every_persisted_path(self):
        env = _env_var_of_each_persisted_path()
        self.assertEqual(sorted(env), sorted(PERSISTED_PATH_CONSTANTS))
        for driver, launcher in _ISOLATED_BY_ENV.items():
            src = (_TESTS_1330_main_driving_tests_ / launcher).read_text(
                encoding="utf-8"
            )
            missing = sorted(c for c, var in env.items() if f'"{var}"' not in src)
            self.assertEqual(
                missing,
                [],
                f"{launcher} runs {driver}, which drives the real "
                "solver_writer.main(), but leaves these persisted paths at their "
                "/opt defaults (nimbus #1330): " + ", ".join(missing),
            )


class TestTheHardcodedPathConventionIsGone(unittest.TestCase):
    """The `/tmp/nonexistent_*.json` pattern, and any new copy of it."""

    def test_no_test_hardcodes_a_persisted_state_path(self):
        offenders = []
        for path in _test_files():
            src = path.read_text(encoding="utf-8")
            if not _calls_main(ast.parse(src)):
                continue
            for const in PERSISTED_PATH_CONSTANTS:
                # A literal path handed to one of these constants in a
                # main()-driving test is the old convention returning.
                idx = src.find(f'"{const}"')
                if idx == -1:
                    continue
                window = src[idx : idx + 200]
                if '"/tmp/' in window or '"/opt/' in window:
                    offenders.append(f"{path.name}:{const}")
        self.assertEqual(
            offenders,
            [],
            "a hardcoded /tmp or /opt path for a persisted state constant in a "
            "main()-driving test. /tmp/nonexistent_*.json is not nonexistent "
            "once main() has written it, which is nimbus #1330's first symptom: "
            + ", ".join(offenders),
        )


class TestTheHelperCoversEveryPersistedPath(unittest.TestCase):
    """If a fifth persisted path is added, isolation must extend to it.

    `PERSISTED_PATH_CONSTANTS` is the helper's own list, so this compares it
    against `solver_writer`'s real module-level constants rather than against a
    second copy that could drift.
    """

    def test_the_helper_knows_about_every_path_constant(self):
        writer = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "nimbus_load"
            / "solver_writer.py"
        )
        tree = ast.parse(writer.read_text(encoding="utf-8"))
        declared = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.endswith("_PATH"):
                    continue
                # An os.environ.get(...) default naming a real file, which is
                # what makes it a persisted path rather than a directory or a
                # computed value.
                if isinstance(node.value, ast.Call):
                    declared.add(target.id)
        # TOKEN_PATH is read-only credentials, never written by main().
        declared.discard("TOKEN_PATH")
        missing = sorted(declared - set(PERSISTED_PATH_CONSTANTS))
        self.assertEqual(
            missing,
            [],
            "solver_writer declares these persisted path constants that "
            "isolated_state_paths() does not redirect, so a main()-driving test "
            "would still write the real file: " + ", ".join(missing),
        )


# --- test_1452_hass_integration_conftest_reuses_isolated_state --------------------------------
# IV&V finding (7ad927b..e497a7c pass, nimbus issue #1452): `tests/hass_
# integration/conftest.py`'s own `solver_state_in_tmp_path` fixture carried a
# second, hand-duplicated copy of `_isolated_state.PERSISTED_PATH_CONSTANTS`'
# name list, and one entry drifted: it spelled `LOAD_ERROR_NOTIFIED_PATH` where
# the real `solver_writer` constant is `LOAD_FORECAST_ERROR_NOTIFIED_ PATH`.
# The fixture's own `if hasattr(solver_writer, name):` guard made the mismatch
# silent -- that one persisted path was never redirected into `tmp_path` for
# any `tests/hass_integration/` run, so a real solve inside that suite could
# read or write the real `/opt/nimbus_solver_load_forecast_ error.txt`
# production path, the exact failure class nimbus issue #1330 exists to
# prevent.

_CONFTEST_PATH = Path(__file__).resolve().parent / "hass_integration" / "conftest.py"


_SRC = _CONFTEST_PATH.read_text(encoding="utf-8")


class TestTheFixtureDelegatesRatherThanDuplicates(unittest.TestCase):
    def test_the_fixture_imports_the_shared_helper(self):
        self.assertIn(
            "from _isolated_state import isolated_state_paths",
            _SRC,
            "solver_state_in_tmp_path no longer imports the shared "
            "_isolated_state.isolated_state_paths() helper -- if it went "
            "back to a hand-copied name list, nimbus #1452's own drift "
            "class (a name silently wrong, guarded by hasattr) can recur.",
        )

    def test_the_fixture_calls_the_shared_helper(self):
        self.assertIn(
            "isolated_state_paths(solver_writer)",
            _SRC,
            "solver_state_in_tmp_path must actually call "
            "isolated_state_paths(solver_writer), not just import it.",
        )

    def test_no_hand_written_persisted_path_name_survives(self):
        """The exact drifted literal from nimbus #1452, and its sibling
        real names, must not appear as hand-typed string literals in this
        file any more -- if they do, someone re-introduced a second,
        independently-maintained copy of the name list."""
        tree = ast.parse(_SRC, filename=str(_CONFTEST_PATH))
        literal_strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # The drifted name itself must never reappear, in any form.
        self.assertNotIn(
            "LOAD_ERROR_NOTIFIED_PATH",
            literal_strings,
            "the drifted name from nimbus #1452 is back as a string "
            "literal in conftest.py",
        )
        # None of the four real persisted-path constant names should be
        # hand-typed here either -- they belong in _isolated_state.py alone.
        for real_name in (
            "PLAN_STATE_PATH",
            "SOLAR_DELIVERY_RATIO_PATH",
            "LOCK_PATH",
            "LOAD_FORECAST_ERROR_NOTIFIED_PATH",
        ):
            with self.subTest(name=real_name):
                self.assertNotIn(
                    real_name,
                    literal_strings,
                    f"{real_name!r} is hand-typed as a string literal in "
                    "conftest.py again -- this fixture should only "
                    "reference the shared _isolated_state list, never "
                    "spell a persisted-path name itself.",
                )


# --- test_1681_no_unguarded_bare_sibling_import -----------------------------------------------
# nimbus #1681: no bare import of a sibling module outside an ImportError
# fallback.

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


EXEMPT_DIRS = {"solver", "ml"}


def _sibling_names() -> set[str]:
    """Modules and packages in the integration directory. A sibling that
    shares a STANDARD-LIBRARY name -- `time.py` and `select.py` are HA
    platforms and must be named so -- is left out: a bare `import time` in
    real HA is the standard library (the integration directory is not on
    sys.path there), so it is not the #1681 bug class."""
    names = set()
    for p in PKG.iterdir():
        if p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
        elif p.suffix == ".py" and p.stem != "__init__":
            names.add(p.stem)
    return names - set(sys.stdlib_module_names)


def _violations() -> list[str]:
    siblings = _sibling_names()
    found = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        if rel.parts[0] in EXEMPT_DIRS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for inner in ast.walk(node):
                    guarded.add(id(inner))
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in siblings:
                    found.append(f"{rel}:{node.lineno} from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in siblings:
                        found.append(f"{rel}:{node.lineno} import {alias.name}")
    return found


def test_no_unguarded_bare_import_of_a_sibling_module():
    violations = _violations()
    assert not violations, (
        "Bare absolute imports of a package sibling outside an `except "
        "ImportError:` fallback resolve only under pytest's sys.path, not in "
        "real HA (#1681). Import relatively first:\n  " + "\n  ".join(violations)
    )


def test_the_1681_site_is_now_guarded():
    """Pins the exact regression: the oracle's imports are relative first."""
    src = (PKG / "solver_inputs" / "controllable_load_history.py").read_text(
        encoding="utf-8"
    )
    assert "from ..solver.elements import" in src
    assert "from ..solver.quality_report import" in src


# --- test_1688_no_raw_once_listener_unload ----------------------------------------------------
# nimbus #1688: no `entry.async_on_unload(hass.bus.async_listen_once(...))`.

PKG_1688_no_raw_once_listene = (
    Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
)


def _raw_once_unloads() -> list[str]:
    found = []
    for path in sorted(PKG_1688_no_raw_once_listene.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_on_unload"
            ):
                continue
            for arg in node.args:
                for inner in ast.walk(arg):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "async_listen_once"
                    ):
                        found.append(
                            f"{path.relative_to(PKG_1688_no_raw_once_listene)}:{node.lineno}"
                        )
    return found


def test_no_once_listener_unsub_is_registered_for_unload():
    assert not _raw_once_unloads(), (
        "a once-listener's unsub registered with async_on_unload is called "
        "twice after the event fires (#1688); use _run_once_started(): "
        + ", ".join(_raw_once_unloads())
    )


def test_both_startup_checks_use_the_safe_helper():
    src = (PKG_1688_no_raw_once_listene / "__init__.py").read_text(encoding="utf-8")
    assert "_run_once_started(hass, entry, _pricing_check)" in src
    assert "_run_once_started(hass, entry, _energy_unit_check)" in src


# --- test_callers_mode_counts_only_real_references --------------------------------------------
# `--callers` must not count a module's own local definition as a reference.

_TOOL = Path(__file__).resolve().parent / "analyse_module_dependencies.py"


_spec = importlib.util.spec_from_file_location("_amd_under_test", _TOOL)


assert _spec and _spec.loader


_amd = importlib.util.module_from_spec(_spec)


sys.modules["_amd_under_test"] = _amd


_spec.loader.exec_module(_amd)


def _names_for(source: str, tmp: Path) -> set[str]:
    f = tmp / "sample.py"
    f.write_text(textwrap.dedent(source), encoding="utf-8")
    return _amd._referenced_names(f)


class TestALocalDefinitionIsNotAReference(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)

    def test_a_module_defining_its_own_logger_is_not_a_caller(self):
        """The exact 13-of-19 case. Every HA module does this."""
        names = _names_for(
            """
            import logging
            _LOGGER = logging.getLogger(__name__)

            def go():
                _LOGGER.warning("hello")
            """,
            self.tmp,
        )
        self.assertNotIn(
            "_LOGGER",
            names,
            "a module that defines and uses its OWN _LOGGER depends on "
            "solver_writer for nothing -- counting it inflated the façade "
            "input 3x on this name",
        )

    def test_reaching_into_the_module_IS_a_caller(self):
        """The six real ones. `sw.X` is how this package reaches solver_writer."""
        names = _names_for(
            """
            from .. import solver_writer as sw

            def go():
                sw._LOGGER.warning("hello")
                sw.ha_get("sensor.x")
            """,
            self.tmp,
        )
        self.assertIn("_LOGGER", names)
        self.assertIn("ha_get", names)

    def test_an_explicit_import_of_the_name_IS_a_caller(self):
        names = _names_for(
            """
            from ..solver_writer import ha_post_state

            def go():
                ha_post_state("sensor.x", "1")
            """,
            self.tmp,
        )
        self.assertIn("ha_post_state", names)

    def test_an_attribute_on_an_unrelated_object_is_not_a_caller(self):
        """`other._LOGGER` says nothing about solver_writer."""
        names = _names_for(
            """
            import some_other_module as other

            def go():
                other._LOGGER.warning("hello")
            """,
            self.tmp,
        )
        self.assertNotIn("_LOGGER", names)

    def test_a_docstring_mention_is_still_not_a_caller(self):
        """The property the tool already had, kept. Docstrings in this codebase
        cross-reference functions constantly."""
        names = _names_for(
            '''
            """See solver_writer.ha_get() for the real thing."""

            def go():
                return 1
            ''',
            self.tmp,
        )
        self.assertNotIn("ha_get", names)


class TestTheFixIsNotVacuous(unittest.TestCase):
    def test_the_aliases_the_package_actually_uses_are_recognised(self):
        """If `_MODULE_ALIASES` ever stopped containing `sw`, every real caller
        would drop to zero and the tool would under-report — which is worse than
        the over-count it replaced, because it would silently retire façades
        that are load-bearing."""
        self.assertIn("sw", _amd._MODULE_ALIASES)
        self.assertIn("solver_writer", _amd._MODULE_ALIASES)

    def test_the_real_tree_now_finds_zero_logger_callers(self):
        """nimbus issue #1301 (spec 001, #1347), Phase 2a: this used to pin
        exactly six real `sw._LOGGER` references (the six `solver_inputs/*`
        modules #1338 flagged as layer violations for exactly this name).
        Phase 2a retargets all six to `solver_shared._LOGGER` directly
        (`_LOGGER` now lives in `solver_shared.py`, not `solver_writer.py`),
        so the real, correct count is now 0 -- this is Phase 2a's own
        success condition for `_LOGGER` specifically, not the tool
        regressing. See `test_the_real_tree_still_finds_the_native_hass_
        callers` below for the alias-set non-vacuity check this test used
        to also cover."""
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        hits = [
            p.relative_to(package).as_posix()
            for p in _amd._py_files(package, skip={"solver_writer.py"})
            if "_LOGGER" in _amd._referenced_names(p)
        ]
        self.assertEqual(
            hits,
            [],
            f"expected zero real sw._LOGGER references now that Phase 2a "
            f"retargets every solver_inputs/*.py module onto "
            f"solver_shared._LOGGER directly, got {hits}",
        )

    def test_the_real_tree_still_finds_the_native_hass_callers(self):
        """Non-vacuity for the alias set, now on a name that is still a real
        `sw.<name>` reference.

        This used to count six `sw._NATIVE_HASS` references. nimbus #1437
        replaced that rebound name with `solver_shared.NATIVE`, read directly
        rather than through the `solver_writer` seam -- so its correct count is
        now 0, which is #1437's own success condition, not the tool regressing.
        `resolve_controllable_load_power_sensor` carries the non-vacuity
        property instead: it is patched on the `solver_writer` module object by
        the v2 tests (tests/v2/reports/test_controllable_load_history.py
        among them), so the three
        modules that call it must go through the seam, and must never drop to 0.
        """
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )

        def hits_for(name):
            return sorted(
                p.relative_to(package).as_posix()
                for p in _amd._py_files(package, skip={"solver_writer.py"})
                if name in _amd._referenced_names(p)
            )

        self.assertEqual(hits_for("_NATIVE_HASS"), [])
        self.assertEqual(
            hits_for("resolve_controllable_load_power_sensor"),
            [
                "solver_dispatch/guard.py",
                "solver_inputs/controllable_load_history.py",
                "solver_inputs/controllable_loads.py",
            ],
        )


class TestPublishedFiguresAreUnchanged(unittest.TestCase):
    """A measurement tool that silently shifts numbers already quoted in specs
    is worse than one that over-counts predictably."""

    def test_phase_2_still_reports_two_production_callers(self):
        package = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        # PHASES is keyed by STRING phase number and maps straight to a tuple
        # of function names -- checked against the module rather than assumed,
        # after an earlier version of this test guessed a dict.
        targets = _amd.PHASES["2"]
        # The tool's own aggregate is `sum(len(v) for v in production.values())`
        # -- (name, file) PAIRS, not distinct files. Reproduced here rather than
        # reinvented: an earlier version of this test counted distinct files,
        # got 1, and "failed" for a reason that had nothing to do with the fix.
        # Both Phase 2 pairs are `services.py` resolving two different names.
        # Skip solver_reports/ as well as solver_writer.py. nimbus issue
        # #1301 Phase 2b/2c actually PERFORMED this extraction, so those
        # functions now LIVE there, and the cluster's own internal sibling
        # references (`sw._soc_discrepancy_stats(...)` inside
        # `_compute_report_for_window()`, and so on) are not external
        # callers. Counting them took this from 2 to 5 and would have read
        # as "the extraction grew the caller surface", which is the opposite
        # of what happened -- the number #1316 published is about how many
        # OTHER production files reach in, and that is still two.
        _relocated = {
            p.relative_to(package).as_posix()
            for p in (package / "solver_reports").glob("*.py")
        }
        pairs = [
            (target, p.relative_to(package).as_posix())
            for p in _amd._py_files(package, skip={"solver_writer.py"})
            if p.relative_to(package).as_posix() not in _relocated
            for target in targets
            if target in _amd._referenced_names(p)
        ]
        self.assertEqual(
            len(pairs),
            2,
            f"#1316 published 'two production callers' for Phase 2; this "
            f"correction must not move it: {sorted(pairs)}",
        )
        self.assertEqual(
            {f for _t, f in pairs},
            {"services.py"},
            "both pairs should be services.py -- if a second file appears, the "
            "figure means something different from what #1316 published",
        )


# --- test_golden_nonvacuity -------------------------------------------------------------------
# Spec 000: the golden scenarios reach the paths they exist for.

GOLDEN = Path(__file__).parent / "golden"


SPIKE = 5.0  # $/kWh: well above any retail price, well below the $23.32 cap


NEMWEB_FOLDERS = sorted(p.name for p in (GOLDEN / "nemweb").iterdir() if p.is_dir())


def _snapshot(name: str) -> dict:
    return json.loads((GOLDEN / "snapshots" / f"{name}.json").read_bytes())


def _battery(cycle: dict) -> dict:
    (post,) = [
        p
        for p in cycle["posted"]
        if p["entity_id"] == "sensor.nimbus_solver_battery_forecast"
    ]
    return post["attributes"]


def test_two_cycles_reads_back_the_plan() -> None:
    snap = _snapshot("guardrail_two_cycles")
    assert "plan_state.json" in snap["files"]
    first, second = (_battery(c) for c in snap["cycles"])
    # Same inputs, five minutes later: the only reason the LP dual moves
    # is the proximal term against the plan cycle 1 saved.
    assert first["forecast"][1]["shadow_price"] != second["forecast"][1]["shadow_price"]


NATIVE_RUN_STATE = "store.nimbus_load_golden_hub_entry_load_run_state.json"


def _diagnostics(cycle: dict) -> dict:
    return _battery(cycle)["solve_diagnostics"]


def test_native_scenario_actually_took_the_native_seam() -> None:
    # Every read in native mode is hass.states.get / the recorder, never
    # urllib. A single REST request here would mean the scenario ran the
    # standalone path with controllable-load subentries nothing could see.
    for name in ("native_controllable_loads", "native_controllable_loads_two_cycles"):
        for cycle in _snapshot(name)["cycles"]:
            methods = {r["method"] for r in cycle["requests"]}
            assert methods, f"{name}: no reads recorded at all"
            assert methods <= {"NATIVE_GET", "NATIVE_HISTORY"}, (
                f"{name}: {sorted(methods - {'NATIVE_GET', 'NATIVE_HISTORY'})}"
            )


def test_native_scenario_builds_both_controllable_load_kinds() -> None:
    # build_controllable_loads() returned ([], [], []) for the whole life of
    # the golden master before #1335; these three counts are that return
    # value, as main() publishes it.
    diag = _diagnostics(_snapshot("native_controllable_loads")["cycles"][0])
    assert diag["n_controllable_loads"] == 2
    assert diag["n_sheddable_loads"] == 1
    assert diag["n_adequacy_loads"] == 1


def test_native_scenario_dispatches_both_loads_through_the_guard() -> None:
    # apply_commanded_state_guard()'s own output stage: one service call per
    # load, each the right service for its device entity's domain.
    cycle = _snapshot("native_controllable_loads")["cycles"][0]
    calls = {(c["domain"], c["service"]): c["data"] for c in cycle["service_calls"]}
    assert calls[("switch", "turn_on")] == {"entity_id": "switch.golden_pool_pump"}
    assert calls[("water_heater", "set_operation_mode")] == {
        "entity_id": "water_heater.golden_hws",
        "operation_mode": "performance",
    }


def test_native_scenario_persists_a_real_run_state_for_each_load() -> None:
    snap = _snapshot("native_controllable_loads")
    store = snap["files"][NATIVE_RUN_STATE]
    assert set(store) == {"golden_sheddable", "golden_deferrable"}
    for key, state in store.items():
        assert state["commanded_state"] is True, key
        assert state["activations_today"] == 1, key
        # The full day-ahead per-load plan (#581), not just period 0.
        assert len(state["plan_forecast"]) == 202, key
    # The deferrable load's power sensor is not configured on its subentry:
    # #768's device-registry discovery resolved it, and the W-reporting
    # sensor was scaled to kW (2950 W -> 2.95 kW, well over the 0.05 kW
    # on-threshold), so this is a real reading and not a default.
    assert store["golden_deferrable"]["currently_on"] is True
    assert store["golden_deferrable"]["plan_target_kwh"] == 3.25


def test_native_two_cycles_does_not_re_dispatch_an_unchanged_command() -> None:
    first, second = _snapshot("native_controllable_loads_two_cycles")["cycles"]
    assert len(first["service_calls"]) == 2
    # Dispatch is change-gated. Cycle 2 wants the same thing, so nothing goes
    # out -- and the debounce/hold path is what decided that, which is only
    # reachable because cycle 1 persisted a state for it to read back.
    assert second["service_calls"] == []
    store = _snapshot("native_controllable_loads_two_cycles")["files"][NATIVE_RUN_STATE]
    for key, state in store.items():
        assert state["delivered_today_kwh"] > 0.0, key
        assert state["activations_today"] == 1, key


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_nemweb_inputs_match_their_provenance(folder: str) -> None:
    base = GOLDEN / "nemweb" / folder
    manifest = [
        json.loads(line) for line in (base / "manifest.jsonl").read_text().splitlines()
    ]
    assert {row["feed"] for row in manifest} >= {"pd7day", "stpasa", "tradingis"}
    assert all(row["url"].startswith("https://nemweb.com.au/") for row in manifest)
    for variant in ("passthrough", "fitted"):
        fc = json.loads(
            gzip.decompress(
                (base / f"nem_pd7day_forecast.{variant}.json.gz").read_bytes()
            )
        )
        for name, sha in fc["provenance"]["inputs"].items():
            assert hashlib.sha256((base / name).read_bytes()).hexdigest() == sha, name
        sources = {p["calibrated_source"] for p in fc["attributes"]["forecast"]}
        assert sources == (
            {"passthrough"} if variant == "passthrough" else sources - {"passthrough"}
        )


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_pd7day_spikes_reach_the_lp_uncalibrated_and_not_calibrated(
    folder: str,
) -> None:
    raw = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
    spikes = [p for p in raw["forecast"] if p["import_price"] >= SPIKE]
    assert spikes, "no PD7DAY spike reached import_price"
    assert any(p["battery_kw"] > 0 for p in spikes), (
        "battery never discharges into a spike"
    )

    # Beyond the first 30 minutes every price is a PD7DAY forecast; inside
    # them the live price and the retail array decide (the real SA1 spike
    # in sa_nemweb_spike_arrived is live, not forecast).
    snap = _snapshot(f"{folder}_fitted")
    horizon = datetime.fromisoformat(snap["cycles"][0]["instant"]) + timedelta(
        minutes=30
    )
    fitted = _battery(snap["cycles"][0])
    late = [
        p
        for p in fitted["forecast"]
        if datetime.fromisoformat(p["time"]) >= horizon and p["import_price"] >= SPIKE
    ]
    assert not late, "calibration left a forecast spike in import_price"


@pytest.mark.parametrize("variant", ["passthrough", "fitted"])
def test_real_spike_arms_the_override(variant: str) -> None:
    attrs = _battery(_snapshot(f"sa_nemweb_spike_arrived_{variant}")["cycles"][0])
    assert attrs["price_spike_active"] is True
    assert attrs["forecast"][0]["import_price"] >= SPIKE
    assert attrs["forecast"][0]["battery_kw"] == 5.0


def test_forecast_spikes_alone_do_not_arm_the_override() -> None:
    for folder in NEMWEB_FOLDERS:
        if folder == "sa_nemweb_spike_arrived":
            continue
        attrs = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
        assert attrs["price_spike_active"] is False, folder


# --- test_pr_deletions_1445 -------------------------------------------------------------------
# nimbus issue #1445: `.github/scripts/check_pr_deletions.py`.

_REPO_ROOT_pr_deletions_1445 = Path(__file__).resolve().parent.parent


_SCRIPT = _REPO_ROOT_pr_deletions_1445 / ".github" / "scripts" / "check_pr_deletions.py"


_WORKFLOW = _REPO_ROOT_pr_deletions_1445 / ".github" / "workflows" / "pr-deletions.yml"


_spec_pr_deletions_1445 = importlib.util.spec_from_file_location(
    "check_pr_deletions", _SCRIPT
)


check = importlib.util.module_from_spec(_spec_pr_deletions_1445)


assert _spec_pr_deletions_1445.loader is not None


_spec_pr_deletions_1445.loader.exec_module(check)


def _git(repo: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
    )
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class TestAcknowledgement(unittest.TestCase):
    def test_paths_and_globs_are_parsed_from_both_line_shapes(self):
        body = (
            "Some prose.\n"
            "Deletes: `tests/golden/snapshots/*.json.gz`, docs/old.md\n"
            "- deletes: legacy/\n"
        )
        self.assertEqual(
            check.acknowledged_patterns(body),
            ["tests/golden/snapshots/*.json.gz", "docs/old.md", "legacy/"],
        )

    def test_a_glob_covers_what_it_matches_and_nothing_else(self):
        deleted = ["tests/golden/snapshots/a.json.gz", "docs/specs/003-x.md"]
        self.assertEqual(
            check.unacknowledged(deleted, ["tests/golden/snapshots/*.json.gz"]),
            ["docs/specs/003-x.md"],
        )

    def test_a_directory_covers_its_contents(self):
        self.assertEqual(check.unacknowledged(["legacy/a/b.py"], ["legacy/"]), [])

    def test_prose_mentioning_a_path_is_not_an_acknowledgement(self):
        """Only a `Deletes:` line counts -- a sentence that happens to name
        the file says nothing about whether deleting it was intended."""
        self.assertEqual(
            check.acknowledged_patterns("We no longer need docs/old.md."), []
        )

    def test_a_dot_directory_path_keeps_its_leading_dot(self):
        """#1691: deleting .github/workflows/x.yml could not be acknowledged,
        because the leading "." was stripped as if it were punctuation."""
        patterns = check.acknowledged_patterns(
            "Deletes: `.github/workflows/nimbus-ivv-daily.yml`."
        )
        self.assertEqual(patterns, [".github/workflows/nimbus-ivv-daily.yml"])
        self.assertEqual(
            check.unacknowledged([".github/workflows/nimbus-ivv-daily.yml"], patterns),
            [],
        )

    def test_empty_body_acknowledges_nothing(self):
        self.assertEqual(check.acknowledged_patterns(""), [])
        self.assertEqual(check.acknowledged_patterns(None), [])


class TestDeletedFilesAgainstTheActualMergeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "keep.md").write_text("keep\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "root")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_file_added_on_main_since_the_branch_is_not_a_deletion(self):
        """Diffing against the base TIP would count this; the merge-base
        must not."""
        _git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "feature.py").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "feature")
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "spec.md").write_text("new on main\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "spec lands on main")
        self.assertEqual(check.deleted_files("main", "feature", self.repo), [])

    def test_a_rename_is_not_a_deletion(self):
        _git(self.repo, "checkout", "-qb", "feature")
        _git(self.repo, "mv", "keep.md", "kept.md")
        _git(self.repo, "commit", "-qm", "rename")
        self.assertEqual(check.deleted_files("main", "feature", self.repo), [])

    def test_the_reset_soft_stale_index_shape_is_caught(self):
        """#1384's mechanism: HEAD moved onto a fresh main by
        `reset --soft`, index still from before a spec landed. The commit's
        parent has the spec; its tree does not."""
        _git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "snap.json").write_text("{}\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "work, cut before the spec existed")
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "spec.md").write_text("approved spec\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "spec merged to main")
        _git(self.repo, "checkout", "-q", "feature")
        _git(self.repo, "reset", "--soft", "main")  # the squash onto a fresh ref
        _git(self.repo, "commit", "-qm", "squashed")
        deleted = check.deleted_files("main", "feature", self.repo)
        self.assertEqual(deleted, ["spec.md"])
        self.assertEqual(check.unacknowledged(deleted, ["snap.json"]), ["spec.md"])


class TestTheRealIncident(unittest.TestCase):
    """Replays PR #1384 from this repository's own history."""

    BASE = "f8d99f0234539319a39218968ce0fa0caf1e990e"  # #1384's recorded base
    HEAD = "70b97acaaadcdb425177769f774de6aa8941182a"  # #1384's single commit

    def setUp(self):
        for sha in (self.BASE, self.HEAD):
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=_REPO_ROOT_pr_deletions_1445,
                capture_output=True,
                check=False,
            )
            if probe.returncode != 0:
                self.skipTest(f"{sha[:7]} not in this clone (shallow checkout)")

    def test_the_intended_acknowledgement_still_leaves_exactly_the_two_specs(self):
        deleted = check.deleted_files(
            self.BASE, self.HEAD, _REPO_ROOT_pr_deletions_1445
        )
        missing = check.unacknowledged(
            deleted,
            check.acknowledged_patterns("Deletes: `tests/golden/snapshots/*.json.gz`"),
        )
        self.assertEqual(
            missing,
            [
                "docs/specs/003-controllable-loads-input.md",
                "docs/specs/004-plan-assembly.md",
            ],
        )


class TestWorkflow(unittest.TestCase):
    def test_body_edits_retrigger_the_check(self):
        """Acknowledging a deletion is a body edit; without `edited` the
        check would stay red after the author fixed it."""
        self.assertIn("edited", _WORKFLOW.read_text(encoding="utf-8"))

    def test_the_body_reaches_the_script_through_the_environment(self):
        """A PR body is untrusted input -- never interpolated into `run:`."""
        text = _WORKFLOW.read_text(encoding="utf-8")
        run_block = text.split("run: |", 1)[1]
        self.assertNotIn("github.event.pull_request.body", run_block)
        self.assertIn("PR_BODY: ${{ github.event.pull_request.body }}", text)


# --- test_1358_thermal_scenario_is_determinate ------------------------------------------------
# The `kind=thermal` golden scenarios stay determinate, and the pair keeps
# discriminating (nimbus issue #1358).

SNAPSHOTS = pathlib.Path(__file__).parent / "golden" / "snapshots"


def _snapshot_1358_thermal_scenario_is(name: str) -> dict:
    return json.loads((SNAPSHOTS / f"{name}.json").read_bytes())


def _dispatch_calls(name: str) -> list[dict]:
    """Every service call the scenario's first cycle made on the tank."""
    return [
        c
        for c in _snapshot_1358_thermal_scenario_is(name)["cycles"][0]["service_calls"]
        if c.get("data", {}).get("entity_id") == st.TANK
    ]


class TestTheDerivation(unittest.TestCase):
    def test_the_constraint_demands_the_energy_the_docstring_says(self):
        self.assertAlmostEqual(
            st.energy_the_constraint_demands(st.THERMAL_TARGET_C), 0.90, places=9
        )
        self.assertAlmostEqual(st.window_capacity_kwh(), 3.0, places=9)

    def test_there_is_real_slack_rather_than_a_knife_edge(self):
        """The whole reason thermal was left out of `scenarios_native`: a
        configuration that only just meets its target sits a rounding error from
        the relaxation path. This one uses under a third of the window."""
        need = st.energy_the_constraint_demands(st.THERMAL_TARGET_C)
        capacity = st.window_capacity_kwh()
        self.assertLess(
            need / capacity,
            0.5,
            f"the thermal scenario now needs {need:.3f} kWh of a {capacity:.1f} kWh "
            f"window ({need / capacity:.0%}). Above half, it is drifting back "
            f"towards the knife edge #1358 exists to avoid -- lower the target or "
            f"widen the window rather than accepting it.",
        )

    def test_the_relaxed_scenario_is_unreachable_by_a_margin(self):
        need = st.energy_the_constraint_demands(st.THERMAL_UNREACHABLE_TARGET_C)
        capacity = st.window_capacity_kwh()
        self.assertGreater(
            need,
            capacity * 1.25,
            f"the relaxation scenario needs {need:.3f} kWh of {capacity:.1f} kWh. "
            f"It must be unreachable by a WIDE margin, not a near miss -- a near "
            f"miss is the knife edge, just approached from the other side.",
        )


class TestThePricesAdmitNoTie(unittest.TestCase):
    def test_every_price_the_lp_sees_is_distinct(self):
        for ladder_name in ("DEFER_LADDER", "HEAT_NOW_LADDER"):
            ladder = getattr(st, ladder_name)
            seen = st.prices_the_lp_sees(ladder)
            with self.subTest(ladder=ladder_name):
                dupes = sorted({p for p in seen if seen.count(p) > 1})
                self.assertEqual(
                    dupes,
                    [],
                    f"{ladder_name} makes the LP see {dupes} more than once "
                    f"(period 0 is the LIVE price, not ladder[0] -- that is how "
                    f"the first draft accidentally tied 0.30 with itself). A tie "
                    f"means the placement is HiGHS's choice, not the price's.",
                )

    def test_defer_ladder_puts_its_cheapest_energy_in_the_future(self):
        seen = st.prices_the_lp_sees(st.DEFER_LADDER)
        self.assertLess(
            min(seen[1:]),
            seen[0],
            "the defer scenario needs cheaper energy later than now, or waiting "
            "is not the optimum and the pair stops discriminating.",
        )

    def test_heat_now_ladder_puts_its_cheapest_energy_now(self):
        seen = st.prices_the_lp_sees(st.HEAT_NOW_LADDER)
        self.assertEqual(
            seen[0],
            min(seen),
            "the heats-now scenario needs NOW to be the cheapest period in the "
            "window, which means every array value must sit above the live "
            f"price {st.LIVE_IMPORT_PRICE}.",
        )


class TestThePairStillDiscriminates(unittest.TestCase):
    """Read from the committed snapshots, so a regeneration that collapses the
    pair fails here instead of passing quietly."""

    def test_the_defer_scenario_records_no_dispatch(self):
        calls = _dispatch_calls("native_thermal_load_defers")
        self.assertEqual(
            calls,
            [],
            f"native_thermal_load_defers dispatched {calls}. Its whole point is "
            f"that the cheapest energy is later, so the optimum is to wait.",
        )

    def test_the_heats_now_scenario_records_a_dispatch(self):
        calls = _dispatch_calls("native_thermal_load_heats_now")
        self.assertEqual(
            [(c["domain"], c["service"]) for c in calls],
            [("water_heater", "set_operation_mode")],
            f"native_thermal_load_heats_now recorded {calls}. Its whole point is "
            f"that now IS the cheapest period, so the optimum is to start.",
        )

    def test_the_two_scenarios_differ_only_in_price(self):
        """Guards the guard. If the two fixtures ever diverge in some other way,
        the pair stops isolating price as the cause of the difference in
        dispatch, and the argument in the module docstring stops holding even
        while both assertions above still pass."""
        defer = st._states(st.DEFER_LADDER)
        heat = st._states(st.HEAT_NOW_LADDER)
        self.assertEqual(
            sorted(defer),
            sorted(heat),
            "the two thermal fixtures no longer publish the same set of entities.",
        )
        differing = sorted(k for k in defer if defer[k] != heat[k])
        self.assertEqual(
            differing,
            [st.PRICE_ARRAY],
            f"the two thermal fixtures differ in {differing}, not only in the "
            f"price forecast. The pair's argument is that price is the ONLY "
            f"difference, so anything else here has to be removed or the "
            f"docstring's claim has to be weakened to match.",
        )


class TestTheRelaxationIsProvable(unittest.TestCase):
    """The relaxation scenario must actually relax, which the first version did
    not (nimbus #1358 follow-up).

    `THERMAL_UNREACHABLE_TARGET_C` was 54.0, derived from an assumed 1-hour /
    3.0 kWh window. The window the scenario really resolves to is periods 0..12
    of the 202-period grid -- 1.5 h and ~4.5 kWh, because the grid coarsens --
    and 54.0 needs ~4.75 kWh, a 6% overshoot on paper that landed on the
    FEASIBLE side in practice. So a scenario named `native_thermal_relaxed`
    solved optimal and never relaxed, sitting on exactly the knife edge the
    module docstring warns against.

    Verified against the real path rather than re-derived: 54.0 does not relax,
    65.0 and 80.0 both do.

    `solver/network.py` logs a WARNING naming each relaxed load, and the harness
    records WARNING and above, so the record CAN show this -- which also
    corrects the first version's claim that the relaxation is "not logged".
    """

    RELAXED = "hard-constrained thermal LP came back infeasible"

    def _warnings(self, name: str) -> list[str]:
        return [
            str(w.get("message", ""))
            for w in _snapshot_1358_thermal_scenario_is(name)["cycles"][0]["warnings"]
        ]

    def test_the_relaxed_scenario_records_the_relaxation(self):
        hits = [
            w for w in self._warnings("native_thermal_relaxed") if self.RELAXED in w
        ]
        self.assertEqual(
            len(hits),
            1,
            "native_thermal_relaxed does not record the relaxation warning, so it "
            "is not exercising the path it is named for -- which is exactly how "
            "the 54.0 target went unnoticed. Raise the target until it does, and "
            "check against the real path rather than the arithmetic.",
        )
        self.assertIn("Tank", hits[0])

    def test_the_two_ordinary_scenarios_do_not_relax(self):
        """The other half: if these ever relax, their own derivation has drifted
        towards infeasible and their placements stop meaning anything."""
        for name in ("native_thermal_load_defers", "native_thermal_load_heats_now"):
            with self.subTest(scenario=name):
                hits = [w for w in self._warnings(name) if self.RELAXED in w]
                self.assertEqual(
                    hits,
                    [],
                    f"{name} relaxed its thermal guarantee. It is supposed to sit "
                    f"well inside feasible -- about 1.15 kWh of a 4.5 kWh window.",
                )

    def test_no_snapshot_records_a_machine_specific_path(self):
        """A relaxation scenario reaches #773's failing-model dump every time,
        which names a temp file. Recording it verbatim would make the snapshot
        pass only on the machine that wrote it -- the once-per-machine failure
        #1330/#1331 found for /tmp state paths. `canonical()` normalises the
        temp directory to `<TMP>/`."""
        for path in sorted(SNAPSHOTS.glob("*.json")):
            with self.subTest(snapshot=path.name):
                raw = path.read_text(encoding="utf-8")
                for marker in ("AppData", "/tmp/", "C:" + chr(92) + chr(92) + "Users"):
                    self.assertNotIn(
                        marker,
                        raw,
                        f"{path.name} records {marker!r}, which is specific to the "
                        f"machine that wrote it.",
                    )


# --- test_gbrt_split_vectorization_equivalence ------------------------------------------------
# Differential equivalence test for the #366 GBRT split-search vectorization.


def _leaf_value_reference(residuals: np.ndarray, quantile: float | None) -> float:
    if quantile is None:
        return float(np.mean(residuals))
    return float(np.percentile(residuals, quantile * 100))


def _build_tree_reference(
    x: np.ndarray,
    residuals: np.ndarray,
    max_depth: int,
    min_samples_leaf: int,
    depth: int = 0,
    quantile: float | None = None,
) -> gbrt._TreeNode:
    """Verbatim copy of the pre-vectorization _build_tree, kept here as an
    independent oracle. Do not "clean up" to match gbrt.py -- the whole
    point is that this file is frozen and gbrt.py is what changed.
    """
    n, n_features = x.shape
    if depth >= max_depth or n < 2 * min_samples_leaf:
        return gbrt._TreeNode(value=_leaf_value_reference(residuals, quantile))

    best_gain = 0.0
    best_feature: int | None = None
    best_threshold: float | None = None
    parent_sse = float(np.sum((residuals - np.mean(residuals)) ** 2))

    for f in range(n_features):
        col = x[:, f]
        order = np.argsort(col)
        sorted_col = col[order]
        sorted_res = residuals[order]

        distinct = np.where(np.diff(sorted_col) > 1e-12)[0]
        if distinct.size == 0:
            continue

        cum_sum = np.cumsum(sorted_res)
        cum_sum_sq = np.cumsum(sorted_res**2)
        total_sum = cum_sum[-1]
        total_sum_sq = cum_sum_sq[-1]

        for i in distinct:
            n_left = i + 1
            n_right = n - n_left
            if n_left < min_samples_leaf or n_right < min_samples_leaf:
                continue
            left_sum = cum_sum[i]
            left_sse = cum_sum_sq[i] - (left_sum**2) / n_left
            right_sum = total_sum - left_sum
            right_sse = (total_sum_sq - cum_sum_sq[i]) - (right_sum**2) / n_right
            gain = parent_sse - (left_sse + right_sse)
            if gain > best_gain:
                best_gain = gain
                best_feature = f
                best_threshold = float((sorted_col[i] + sorted_col[i + 1]) / 2.0)

    if best_feature is None or best_gain <= 1e-9:
        return gbrt._TreeNode(value=_leaf_value_reference(residuals, quantile))

    mask = x[:, best_feature] <= best_threshold
    return gbrt._TreeNode(
        feature=best_feature,
        threshold=best_threshold,
        left=_build_tree_reference(
            x[mask], residuals[mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
        right=_build_tree_reference(
            x[~mask], residuals[~mask], max_depth, min_samples_leaf, depth + 1, quantile
        ),
    )


def _tree_to_tuple(node: gbrt._TreeNode) -> tuple:
    if node.is_leaf():
        return ("leaf", node.value)
    return (
        "split",
        node.feature,
        node.threshold,
        _tree_to_tuple(node.left),
        _tree_to_tuple(node.right),
    )


def _assert_trees_identical(a: gbrt._TreeNode, b: gbrt._TreeNode) -> None:
    assert _tree_to_tuple(a) == _tree_to_tuple(b)


CASES = [
    ("small_1feature", 20, 1, 3, 2, None),
    ("small_multifeature", 40, 4, 3, 2, None),
    ("min_samples_leaf_boundary", 12, 2, 4, 5, None),
    ("many_ties_low_cardinality", 60, 3, 4, 3, None),
    ("wide_features", 30, 10, 3, 2, None),
    ("large_ish", 400, 5, 4, 5, None),
    ("quantile_low", 60, 3, 3, 3, 0.1),
    ("quantile_high", 60, 3, 3, 3, 0.9),
    ("quantile_median", 60, 3, 3, 3, 0.5),
]


@pytest.mark.parametrize(
    "name,n_rows,n_features,max_depth,min_samples_leaf,quantile", CASES
)
def test_vectorized_matches_reference_across_seeds(
    name, n_rows, n_features, max_depth, min_samples_leaf, quantile
):
    for seed in range(30):
        # nimbus issue #1282. Was `hash(name) % 997`, and Python RANDOMISES
        # string hashing per process -- so despite the name, this test
        # explored a DIFFERENT random space on every run. That is what made
        # it fail once in CI on a diff that touched only manifest.json and
        # CHANGELOG.md, then pass on re-run of the same commit.
        #
        # This repo already documents the identical trap in production code:
        # test_1217_retrain_is_staggered.py's own
        # `test_it_does_not_use_pythons_randomised_string_hash`, written when
        # #1217's retrain stagger deliberately chose sha256 over hash() so a
        # subentry lands on the same minute across restarts.
        #
        # crc32 is stable across processes and interpreters, so a CI failure
        # here is now reproducible from the seed alone.
        rng = np.random.default_rng(seed * 1000 + zlib.crc32(name.encode()) % 997)
        x = rng.integers(0, 6, size=(n_rows, n_features)).astype(np.float64)
        residuals = rng.normal(size=n_rows)

        expected = _build_tree_reference(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        actual = gbrt._build_tree(
            x.copy(), residuals.copy(), max_depth, min_samples_leaf, 0, quantile
        )
        _assert_trees_identical(expected, actual)


def test_the_known_tie_break_divergence_is_an_exact_tie_not_a_wrong_split():
    """nimbus issue #1282: the one real divergence the randomised seeding hid.

    A deterministic sweep of the full seed space this test samples from --
    269,190 (case, seed) combinations across every entry in CASES -- found
    exactly ONE tree mismatch: `wide_features` at `default_rng(13631)`.

    It is a TIE, not a wrong split, and this test is what says so rather than
    leaving "the trees differ" as the whole story:

        node root.R.L, 5 rows
          reference : feature 2, threshold 0.5  -> left rows {1, 2, 4}
          vectorized: feature 1, threshold 2.5  -> left rows {0, 3}
          gain, both : 1.316964862796671   (bit-identical)

    The two features cut the node into complementary halves with the same
    sum-of-squares gain, which is why the child leaf values come out equal but
    swapped. Both trees are correct; the paths break the tie on different
    features.

    So this asserts the property that actually matters -- the two candidate
    splits are exactly equally good -- rather than asserting the trees match,
    which they legitimately do not here.

    **What this deliberately does NOT do:** change the tie-break in
    `gbrt._build_tree` so the paths agree. That would be a change to the
    trained model's output on real installs to satisfy a test, and it is a
    decision for whoever owns the ML path, not a side effect of a flake
    investigation. Recorded on #1282 instead.
    """
    rng = np.random.default_rng(13631)
    x = rng.integers(0, 6, size=(30, 10)).astype(np.float64)
    residuals = rng.normal(size=30)

    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)

    # The divergence is real and still present -- if it ever disappears, the
    # tie-break was changed and this test should be revisited deliberately.
    assert _tree_to_tuple(expected) != _tree_to_tuple(actual), (
        "the known #1282 divergence at seed 13631 is gone -- if the tie-break "
        "was made to agree, delete this test and say so on that issue"
    )

    # Descend to the disputed node and prove the two candidate splits tie.
    node = expected
    subset_x, subset_r = x, residuals
    for step in ("R", "L"):
        mask = subset_x[:, node.feature] <= node.threshold
        keep = ~mask if step == "R" else mask
        subset_x, subset_r = subset_x[keep], subset_r[keep]
        node = node.right if step == "R" else node.left

    def _gain(feature: int, threshold: float) -> float:
        mask = subset_x[:, feature] <= threshold
        left, right = subset_r[mask], subset_r[~mask]

        def ss(a: np.ndarray) -> float:
            return float(a.sum() ** 2 / len(a)) if len(a) else 0.0

        return ss(left) + ss(right)

    assert _gain(2, 0.5) == _gain(1, 2.5), (
        "the #1282 divergence was only acceptable because the two splits are "
        "an EXACT tie; if their gains now differ, one path is choosing a "
        "genuinely worse split and this is a real bug"
    )


def test_the_seeding_is_reproducible_across_processes():
    """The root cause of #1282's flakiness, pinned.

    `hash()` on a str is salted per process, so a test seeded from it samples
    a different space on every run -- which is how a genuine divergence sat
    undetected and then surfaced on an unrelated release PR.

    Parsed rather than grepped: a text search would match the explanatory
    comment above the line and pass for the wrong reason. The same technique
    `test_1217_retrain_is_staggered.py` uses for the identical trap in
    production code.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(
        textwrap.dedent(
            inspect.getsource(test_vectorized_matches_reference_across_seeds)
        )
    )
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "hash" not in called, (
        "this test seeds from Python's randomised string hash, so it explores "
        "a different space every run and its failures are not reproducible "
        "(nimbus issue #1282)"
    )


def test_vectorized_matches_reference_continuous_features():
    for seed in range(20):
        rng = np.random.default_rng(seed + 5000)
        x = rng.normal(size=(150, 6))
        residuals = rng.normal(size=150) * 3.0

        expected = _build_tree_reference(x.copy(), residuals.copy(), 4, 5, 0, None)
        actual = gbrt._build_tree(x.copy(), residuals.copy(), 4, 5, 0, None)
        _assert_trees_identical(expected, actual)


def test_vectorized_matches_reference_all_identical_feature_values():
    # No valid split anywhere for this feature -- must fall through to a leaf,
    # not raise on an all-invalid/empty gain array.
    rng = np.random.default_rng(42)
    x = np.ones((10, 2))
    residuals = rng.normal(size=10)
    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)
    _assert_trees_identical(expected, actual)
    assert actual.is_leaf()


def test_vectorized_matches_reference_exact_tie_gain_prefers_first_feature():
    # Two duplicate feature columns should tie on gain at every split point;
    # the reference picks the first (lowest-index) feature on a tie because
    # it only replaces the champion on strict `>`. The vectorized version
    # must reproduce this exact tie-break, not e.g. numpy's own argmax
    # semantics under a different reduction order.
    rng = np.random.default_rng(7)
    col = rng.integers(0, 5, size=30).astype(np.float64)
    x = np.column_stack([col, col.copy(), rng.integers(0, 5, size=30)])
    residuals = rng.normal(size=30)

    expected = _build_tree_reference(x.copy(), residuals.copy(), 3, 2, 0, None)
    actual = gbrt._build_tree(x.copy(), residuals.copy(), 3, 2, 0, None)
    _assert_trees_identical(expected, actual)


def test_vectorized_matches_reference_full_fit_predictions():
    # End-to-end through GBRT.fit()/predict(), not just _build_tree directly.
    rng = np.random.default_rng(99)
    x = rng.normal(size=(200, 4))
    y = x[:, 0] * 2.0 - x[:, 1] + rng.normal(size=200) * 0.1

    model = gbrt.GBRT(n_estimators=15, max_depth=3, min_samples_leaf=5)
    model.fit(x, y)
    preds = model.predict(x)

    # Cross-check against the reference oracle by monkeypatching _build_tree
    # for a fresh model fit over the same data.
    original = gbrt._build_tree
    gbrt._build_tree = _build_tree_reference
    try:
        reference_model = gbrt.GBRT(n_estimators=15, max_depth=3, min_samples_leaf=5)
        reference_model.fit(x, y)
        reference_preds = reference_model.predict(x)
    finally:
        gbrt._build_tree = original

    np.testing.assert_array_equal(preds, reference_preds)
