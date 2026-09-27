"""Tests for `tests/gates/noop_patches.py` (spec 000, Part C, the #1316 gate).

Reproduces the exact #1316 shape on a synthetic fixture: a tiny fake
production module with a `helper()` a `compute()` calls, patched by a test
via `patch.object(module, "helper", ...)`. One head version keeps `helper()`
callable through the same module (the patch keeps working); the other moves
`helper()` to a new module and re-points `compute()` at it by qualified
reference, leaving the test's own `patch.object(fake_prod, "helper", ...)`
site silently unreachable -- the test file itself does not change at all
between the two head versions, which is the point: this is exactly the case
`assertions_unchanged.py` cannot see (the test text is identical), and only
runtime instrumentation can.

Uses `--base-dir`/`--head-dir` (skips `git worktree`) exactly like
`tests/test_gates_assertions_unchanged.py`, for the same reason: fast,
hermetic, no dependency on `git worktree` behaving a particular way in every
CI sandbox. The real `git worktree` code path is shared verbatim with
`assertions_unchanged.py` (same `_worktree`/`_remove_worktree` shape) and was
exercised manually against real repo history as part of this PR's own
validation (see the PR body).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import _solver_path  # noqa: F401
from gates import noop_patches as np

REPO_ROOT = Path(__file__).resolve().parent.parent

_FAKE_PROD_BASE = '''
"""Fake production module, base version -- helper() lives here and is
called by its bare name, so patching this module's own `helper` attribute
is exactly where `compute()` looks it up."""


def helper():
    return "real"


def compute():
    return helper()
'''

_FAKE_PROD_HEAD_UNCHANGED = _FAKE_PROD_BASE

_FAKE_PROD_HEAD_MOVED = '''
"""Fake production module, head version -- helper() has moved to
fake_prod_moved.py, and compute() now resolves it through that module
instead of its own former bare name. `helper` is still re-exported here
(exactly the plan's own "facade criterion" -- a name a test's monkeypatch
target still resolves keeps a re-export) so `patch.object(fake_prod,
"helper", ...)` still succeeds -- it just no longer intercepts anything,
because compute() no longer reads THIS module's `helper` name. This is the
real #1316 shape: the attribute exists, the patch call doesn't error, and
the test passes having exercised the real function."""

from fake_prod_moved import helper  # noqa: F401 -- kept only as a patch target
import fake_prod_moved


def compute():
    return fake_prod_moved.helper()
'''

_FAKE_PROD_MOVED = '''
"""Where helper() now lives, after the refactor."""


def helper():
    return "real"
'''

_FAKE_TEST = '''
"""Patches fake_prod.helper and calls fake_prod.compute() -- unchanged
across both head fixtures on purpose. A refactor that breaks the patch
without touching this file is exactly what noop_patches.py exists to catch;
assertions_unchanged.py would see this file as byte-identical and correctly
report nothing.
"""

import unittest
from unittest.mock import patch

import fake_prod


class TestCompute(unittest.TestCase):
    def test_compute_uses_the_patched_helper(self):
        with patch.object(fake_prod, "helper", return_value="mocked"):
            result = fake_prod.compute()
        # Deliberately loose: true whether the mock or the real helper() ran.
        # This is exactly why a naive "did the test still pass" check misses
        # the #1316 regression -- this assertion passes either way.
        self.assertIsInstance(result, str)
'''


def _build_tree(root: Path, prod_content: str, moved_content: str | None) -> None:
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "fake_prod.py").write_text(prod_content, encoding="utf-8")
    if moved_content is not None:
        (tests_dir / "fake_prod_moved.py").write_text(moved_content, encoding="utf-8")
    (tests_dir / "test_fake_prod.py").write_text(_FAKE_TEST, encoding="utf-8")


class TestStaticHelpers(unittest.TestCase):
    def test_file_has_patch_sites_detects_patch_object(self):
        self.assertTrue(np._file_has_patch_sites("with patch.object(x, 'y'): pass"))
        self.assertTrue(np._file_has_patch_sites("monkeypatch.setattr(x, 'y', z)"))
        self.assertFalse(np._file_has_patch_sites("def test_nothing(): assert True"))


class TestNoopPatchesOnSyntheticFixture(unittest.TestCase):
    """The two required cases: caught on the bad commit, clean on the good one."""

    def test_1316_shape_is_caught(self):
        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            base_root, head_root = Path(base_tmp), Path(head_tmp)
            _build_tree(base_root, _FAKE_PROD_BASE, moved_content=None)
            _build_tree(
                head_root, _FAKE_PROD_HEAD_MOVED, moved_content=_FAKE_PROD_MOVED
            )

            base_data = np._run_tracked(base_root, ["tests/test_fake_prod.py"])
            head_data = np._run_tracked(head_root, ["tests/test_fake_prod.py"])

        base_records = base_data["records"]
        self.assertTrue(
            base_records,
            "the base run recorded nothing at all -- instrumentation broken",
        )
        test_id = next(iter(base_records))
        self.assertTrue(
            base_records[test_id].get("fake_prod.helper"),
            f"the patch was never even touched on base: {base_records}",
        )

        report = np.compare_runs(base_data, head_data)
        self.assertEqual(len(report.regressions), 1, report.regressions)
        self.assertEqual(report.regressions[0].site, "fake_prod.helper")
        self.assertIn(
            "test_compute_uses_the_patched_helper", report.regressions[0].test_id
        )

    def test_unchanged_production_code_reports_clean(self):
        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            base_root, head_root = Path(base_tmp), Path(head_tmp)
            _build_tree(base_root, _FAKE_PROD_BASE, moved_content=None)
            _build_tree(head_root, _FAKE_PROD_HEAD_UNCHANGED, moved_content=None)

            base_data = np._run_tracked(base_root, ["tests/test_fake_prod.py"])
            head_data = np._run_tracked(head_root, ["tests/test_fake_prod.py"])

        report = np.compare_runs(base_data, head_data)
        self.assertEqual(report.regressions, [])


class TestCliExitCodes(unittest.TestCase):
    def _run_cli(self, base_root: Path, head_root: Path) -> subprocess.CompletedProcess:
        script = REPO_ROOT / "tests" / "gates" / "noop_patches.py"
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "--base",
                "unused",
                "--head",
                "unused",
                "--base-dir",
                str(base_root),
                "--head-dir",
                str(head_root),
                "--tests",
                "tests/test_fake_prod.py",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exit_one_on_the_1316_shape(self):
        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            base_root, head_root = Path(base_tmp), Path(head_tmp)
            _build_tree(base_root, _FAKE_PROD_BASE, moved_content=None)
            _build_tree(
                head_root, _FAKE_PROD_HEAD_MOVED, moved_content=_FAKE_PROD_MOVED
            )
            result = self._run_cli(base_root, head_root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("fake_prod.helper", result.stdout)

    def test_exit_zero_when_unchanged(self):
        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            base_root, head_root = Path(base_tmp), Path(head_tmp)
            _build_tree(base_root, _FAKE_PROD_BASE, moved_content=None)
            _build_tree(head_root, _FAKE_PROD_HEAD_UNCHANGED, moved_content=None)
            result = self._run_cli(base_root, head_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


class TestAgainstTheRealRepo(unittest.TestCase):
    """base == head against a real slice of this repo's own patch-heavy tests.

    Scoped to a handful of real files (not the full ~80-file/512-site set
    spec 000's own table measures) so this stays fast enough to run in the
    normal stub-based suite -- this tool executes the tests it checks, unlike
    `assertions_unchanged.py`'s pure-parse approach, so the cost scales with
    how much of the suite is selected. The full real set was run by hand as
    part of this PR's own validation (see the PR body for the result); this
    automated slice is the standing regression check that keeps running.
    """

    _REAL_FILES: ClassVar[list[str]] = [
        "tests/test_household_mode_presets.py",
        "tests/test_fetch_solver_config.py",
        "tests/test_controllable_load_power_sensor_discovery.py",
    ]

    def test_zero_regressions_on_a_real_slice_compared_to_itself(self):
        base_data = np._run_tracked(REPO_ROOT, self._REAL_FILES)
        head_data = np._run_tracked(REPO_ROOT, self._REAL_FILES)
        self.assertTrue(
            base_data["records"],
            "no patch sites recorded at all -- check instrumentation",
        )
        report = np.compare_runs(base_data, head_data)
        self.assertEqual(
            report.regressions,
            [],
            "comparing a real, unchanged file set against itself must never report a regression",
        )


if __name__ == "__main__":
    unittest.main()
