"""nimbus issue #1445: `.github/scripts/check_pr_deletions.py`.

The decisive test is the last class: the real #1384 commit, with the
acknowledgement its author would genuinely have written for the intended
`.json.gz` migration, must still fail -- on exactly the two specs, and
nothing else. That is the incident this check exists for, replayed from
history rather than described.

Everything else runs against a throwaway repository built inside the
test, including a reproduction of the `reset --soft` stale-index shape,
so no result depends on CI's clone depth and nothing outside the temp
directory can move.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "check_pr_deletions.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "pr-deletions.yml"

_spec = importlib.util.spec_from_file_location("check_pr_deletions", _SCRIPT)
check = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(check)


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
                cwd=_REPO_ROOT,
                capture_output=True,
                check=False,
            )
            if probe.returncode != 0:
                self.skipTest(f"{sha[:7]} not in this clone (shallow checkout)")

    def test_the_intended_acknowledgement_still_leaves_exactly_the_two_specs(self):
        deleted = check.deleted_files(self.BASE, self.HEAD, _REPO_ROOT)
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


if __name__ == "__main__":
    unittest.main()
