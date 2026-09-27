"""Tests for `tests/gates/assertions_unchanged.py` (spec 000, Part C).

Three things are proven here:

1. A synthetic "bad refactor" (an existing test's assertion value silently
   changed) is caught, and its `--allowed-changes` escape hatch works.
2. A synthetic "good refactor" (only NEW assertions/tests added, nothing
   existing edited) passes clean.
3. Run for real against THIS repo's own `tests/` directory, comparing it
   against itself (base == head), the tool reports zero changes across
   every real test file here -- the false-positive sanity check the task
   asked for. Comparing a tree against itself can't prove the tool detects
   every real change (that's what 1 covers, on a synthetic fixture built
   for the purpose), but it does prove the tool parses and diffs 400+ real,
   syntactically varied test files without crashing or manufacturing a
   spurious difference -- which is the actual risk with a large real corpus.

The synthetic cases use `compare_trees()`/`--base-dir`/`--head-dir` directly
against temp directories rather than real git refs, so they run in
milliseconds and don't depend on `git worktree` behaving a particular way in
every CI sandbox. `git worktree add --detach` is real, supported behaviour of
the tool (it's the default code path with no `--base-dir`/`--head-dir`
override) -- it is exercised manually against real repo history as part of
this PR's own validation, described in the PR body, rather than inside this
suite, to keep the stub-based suite's own runtime and git dependencies
unchanged.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
from gates import assertions_unchanged as au

REPO_ROOT = Path(__file__).resolve().parent.parent

_GOOD_TEST_BASE = '''
"""A tiny fake test module, base version."""
import unittest


class TestThing(unittest.TestCase):
    def test_adds_to_two(self):
        self.assertEqual(1 + 1, 2)
        self.assertTrue(True)
'''

_GOOD_TEST_HEAD_ONLY_ADDITIONS = '''
"""A tiny fake test module, head version -- extra test added, nothing
existing touched."""
import unittest


class TestThing(unittest.TestCase):
    def test_adds_to_two(self):
        self.assertEqual(1 + 1, 2)
        self.assertTrue(True)

    def test_a_brand_new_thing(self):
        self.assertEqual(2 + 2, 4)
'''

_BAD_TEST_HEAD_CHANGED_ASSERTION = '''
"""A tiny fake test module, head version -- an existing assertion's
expected value silently changed (the failure mode this gate exists to
catch: a "refactor" that loosens what a test checks)."""
import unittest


class TestThing(unittest.TestCase):
    def test_adds_to_two(self):
        self.assertEqual(1 + 1, 3)
        self.assertTrue(True)
'''

_BAD_TEST_HEAD_REMOVED_ASSERTION = '''
"""A tiny fake test module, head version -- an existing assertion
disappeared entirely (also a real weakening, not just an edit)."""
import unittest


class TestThing(unittest.TestCase):
    def test_adds_to_two(self):
        self.assertEqual(1 + 1, 2)
'''

_REFORMATTED_ONLY = '''
"""Same assertions, reflowed onto more lines -- must NOT be flagged."""
import unittest


class TestThing(unittest.TestCase):
    def test_adds_to_two(self):
        self.assertEqual(
            1 + 1,
            2,
        )
        self.assertTrue(
            True
        )
'''


def _write_tree(root: Path, filename: str, content: str) -> None:
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / filename).write_text(content, encoding="utf-8")


class TestAssertionCollection(unittest.TestCase):
    def test_bare_assert_self_assert_star_and_pytest_raises_all_collected(self):
        source = """
import unittest
import pytest


class TestX(unittest.TestCase):
    def test_various(self):
        assert 1 == 1
        self.assertEqual(2, 2)
        with pytest.raises(ValueError):
            raise ValueError()
        mock_obj = object()
        mock_obj.assert_called_once()
"""
        import ast

        tree = ast.parse(source)
        funcs = au._iter_test_functions(tree)
        self.assertEqual(len(funcs), 1)
        qualname, node = funcs[0]
        self.assertEqual(qualname, "TestX.test_various")
        found = au._collect_assertions(source, node, qualname)
        # bare assert, self.assertEqual, pytest.raises(...), mock_obj.assert_called_once()
        self.assertEqual(len(found), 4)


class TestBadRefactorIsCaught(unittest.TestCase):
    def _compare(self, base_content: str, head_content: str, allowed=None):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            _write_tree(Path(base_tmp), "test_fixture.py", base_content)
            _write_tree(Path(head_tmp), "test_fixture.py", head_content)
            return au.compare_trees(Path(base_tmp), Path(head_tmp), allowed or set())

    def test_changed_assertion_value_is_flagged(self):
        report = self._compare(_GOOD_TEST_BASE, _BAD_TEST_HEAD_CHANGED_ASSERTION)
        self.assertEqual(len(report.changes), 1)
        change = report.changes[0]
        self.assertEqual(change.qualname, "TestThing.test_adds_to_two")
        self.assertIn("2", change.old.text)
        self.assertIn("3", change.new.text)

    def test_removed_assertion_is_flagged(self):
        report = self._compare(_GOOD_TEST_BASE, _BAD_TEST_HEAD_REMOVED_ASSERTION)
        self.assertEqual(len(report.changes), 1)
        change = report.changes[0]
        self.assertIsNotNone(change.old)
        self.assertIsNone(change.new)

    def test_only_additions_pass_clean(self):
        report = self._compare(_GOOD_TEST_BASE, _GOOD_TEST_HEAD_ONLY_ADDITIONS)
        self.assertEqual(report.changes, [])
        self.assertEqual(report.files_compared, 1)

    def test_pure_reformat_is_not_flagged(self):
        """Structural comparison, not raw text -- see the tool's own
        module docstring for why."""
        report = self._compare(_GOOD_TEST_BASE, _REFORMATTED_ONLY)
        self.assertEqual(report.changes, [])

    def test_allowed_changes_suppresses_a_listed_line(self):
        report_unsuppressed = self._compare(
            _GOOD_TEST_BASE, _BAD_TEST_HEAD_CHANGED_ASSERTION
        )
        self.assertEqual(len(report_unsuppressed.changes), 1)
        old_line = report_unsuppressed.changes[0].old.lineno
        allowed = {f"tests/test_fixture.py:{old_line}"}
        report = self._compare(
            _GOOD_TEST_BASE, _BAD_TEST_HEAD_CHANGED_ASSERTION, allowed=allowed
        )
        self.assertEqual(report.changes, [])
        self.assertEqual(len(report.suppressed), 1)

    def test_allowed_changes_by_qualified_test_name(self):
        allowed = {"tests/test_fixture.py::TestThing.test_adds_to_two"}
        report = self._compare(
            _GOOD_TEST_BASE, _BAD_TEST_HEAD_CHANGED_ASSERTION, allowed=allowed
        )
        self.assertEqual(report.changes, [])
        self.assertEqual(len(report.suppressed), 1)

    def test_new_file_in_head_only_is_out_of_scope(self):
        """A wholly new test file is fine -- only files present at both
        ends are compared."""
        import tempfile

        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            _write_tree(Path(base_tmp), "test_fixture.py", _GOOD_TEST_BASE)
            _write_tree(Path(head_tmp), "test_fixture.py", _GOOD_TEST_BASE)
            _write_tree(
                Path(head_tmp),
                "test_brand_new_file.py",
                _BAD_TEST_HEAD_CHANGED_ASSERTION,
            )
            report = au.compare_trees(Path(base_tmp), Path(head_tmp), set())
            self.assertEqual(report.changes, [])
            self.assertEqual(report.files_compared, 1)


class TestAllowedChangesParsing(unittest.TestCase):
    def test_json_list(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(["tests/test_x.py:10", "tests/test_y.py::TestA.test_b"], f)
            path = f.name
        entries = au._load_allowed_changes(path)
        self.assertEqual(
            entries, {"tests/test_x.py:10", "tests/test_y.py::TestA.test_b"}
        )

    def test_markdown_prose_is_scraped(self):
        import tempfile

        content = (
            "## Facade decision\n\n"
            "Retargets `tests/test_x.py:42`'s assertion to the new module, "
            "and `tests/test_y.py::TestA.test_b` moves its import.\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(content)
            path = f.name
        entries = au._load_allowed_changes(path)
        self.assertIn("tests/test_x.py:42", entries)
        self.assertIn("tests/test_y.py::TestA.test_b", entries)

    def test_no_spec_means_empty_allowlist(self):
        self.assertEqual(au._load_allowed_changes(None), set())


class TestCliExitCodes(unittest.TestCase):
    """Exercises the actual `python tests/gates/assertions_unchanged.py`
    entry point (via --base-dir/--head-dir, not git worktrees -- see the
    module docstring)."""

    def _run_cli(self, base_root: Path, head_root: Path) -> subprocess.CompletedProcess:
        script = REPO_ROOT / "tests" / "gates" / "assertions_unchanged.py"
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
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exit_zero_on_clean_diff(self):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            _write_tree(Path(base_tmp), "test_fixture.py", _GOOD_TEST_BASE)
            _write_tree(
                Path(head_tmp), "test_fixture.py", _GOOD_TEST_HEAD_ONLY_ADDITIONS
            )
            result = self._run_cli(Path(base_tmp), Path(head_tmp))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_exit_one_on_changed_assertion(self):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as base_tmp,
            tempfile.TemporaryDirectory() as head_tmp,
        ):
            _write_tree(Path(base_tmp), "test_fixture.py", _GOOD_TEST_BASE)
            _write_tree(
                Path(head_tmp), "test_fixture.py", _BAD_TEST_HEAD_CHANGED_ASSERTION
            )
            result = self._run_cli(Path(base_tmp), Path(head_tmp))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)


class TestAgainstTheRealRepo(unittest.TestCase):
    """base == head against this repo's own real tests/ directory.

    See the module docstring's item 3: this cannot prove the tool detects a
    real change (the synthetic fixtures above do that), but it does prove
    the tool parses and diffs every real test file here --400+ files with
    real, varied syntax -- without crashing or manufacturing a false
    positive, which was the concrete risk called out for this gate.
    """

    def test_zero_changes_and_zero_crashes_across_the_real_suite(self):
        report = au.compare_trees(REPO_ROOT, REPO_ROOT, set())
        self.assertEqual(
            report.changes,
            [],
            "comparing the real tests/ tree against itself must never report a change",
        )
        # Sanity that this actually scanned real content, not an empty dir.
        self.assertGreater(report.files_compared, 100)


if __name__ == "__main__":
    unittest.main()
