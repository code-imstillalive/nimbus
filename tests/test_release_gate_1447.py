"""nimbus issue #1447: the release gate in `.github/scripts/check_release_validated.py`.

Three things are pinned here, each for a reason that has already cost a
release:

1. **The gate's patterns are the #594 guard's patterns.** If they drift,
   the gate either publishes a release CI would reject or refuses one CI
   accepts -- and the second is how a guard gets switched off.
2. **The section check refuses what it should and accepts the honest
   answers**, including "not claimed", which the RELEASE VALIDATION
   directive treats as first-class. A gate that makes the honest answer
   harder than a false one would be worse than no gate.
3. **The code-identity check catches a change between validation and
   tag.** That is the real hazard -- a fix pushed after devhub was
   checked -- and it is exercised against a throwaway git repository
   built inside the test, never this checkout, so the test cannot depend
   on CI's clone depth and cannot touch the real machine (the #1405
   reaper's first version deleted real temp trees from its own suite).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATE_PATH = _REPO_ROOT / ".github" / "scripts" / "check_release_validated.py"
_RELEASE_YML = _REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gate = _load("check_release_validated", _GATE_PATH)
guard = _load(
    "changelog_release_validation_guard",
    _REPO_ROOT / "tests" / "test_changelog_release_validation.py",
)

_GOOD_CONSUMER = "- Consumer check: nothing user-visible changed on the dashboard.\n"


class TestPatternsMatchTheChangelogGuard(unittest.TestCase):
    def test_validation_pattern_is_identical(self):
        self.assertEqual(gate.VALIDATION_RE.pattern, guard._VALIDATION_RE.pattern)
        self.assertEqual(gate.VALIDATION_RE.flags, guard._VALIDATION_RE.flags)

    def test_consumer_pattern_is_identical(self):
        self.assertEqual(gate.CONSUMER_RE.pattern, guard._CONSUMER_RE.pattern)
        self.assertEqual(gate.CONSUMER_RE.flags, guard._CONSUMER_RE.flags)

    def test_prose_stripping_is_identical(self):
        sample = (
            "a `Devhub validation:` span\n```\nDevhub validation: fenced\n```\nprose"
        )
        self.assertEqual(gate.prose_only(sample), guard._prose_only(sample))


class TestSectionCheck(unittest.TestCase):
    def test_missing_validation_line_is_refused(self):
        problems, _ = gate.check_section("### Fixed\n- a thing\n" + _GOOD_CONSUMER)
        self.assertTrue(any("Devhub validation" in p for p in problems))

    def test_missing_consumer_line_is_refused(self):
        problems, _ = gate.check_section(
            "- Devhub validation: not claimed, because tests-only.\n"
        )
        self.assertTrue(any("Consumer check" in p for p in problems))

    def test_validation_without_citation_or_not_claimed_is_refused(self):
        """The line exists but says nothing checkable about WHAT was validated."""
        problems, cited = gate.check_section(
            "- Devhub validation: restart clean, solve optimal.\n" + _GOOD_CONSUMER
        )
        self.assertIsNone(cited)
        self.assertTrue(any("names no commit" in p for p in problems))

    def test_not_claimed_is_a_first_class_answer(self):
        problems, cited = gate.check_section(
            "- Devhub validation: not claimed, because this release is tests-only.\n"
            + _GOOD_CONSUMER
        )
        self.assertEqual(problems, [])
        self.assertIsNone(cited)

    def test_a_cited_commit_is_extracted(self):
        problems, cited = gate.check_section(
            "- Devhub validation: installed `main` at 9d3be28 via HACS, restarted,\n"
            "  solve optimal.\n" + _GOOD_CONSUMER
        )
        self.assertEqual(problems, [])
        self.assertEqual(cited, "9d3be28")

    def test_a_citation_on_a_wrapped_continuation_line_is_found(self):
        """The real file wraps these lines; the hash can land on line two."""
        _, cited = gate.check_section(
            "- Devhub validation: **confirmed live on devhub (HA 2026.9.3)** --\n"
            "  installed at commit abcdef1234 and restarted.\n" + _GOOD_CONSUMER
        )
        self.assertEqual(cited, "abcdef1234")

    def test_a_commit_in_a_different_bullet_is_not_a_citation(self):
        """A hash elsewhere in the entry says nothing about the validation."""
        problems, cited = gate.check_section(
            "- Devhub validation: restart clean.\n"
            "- Fixed at 1234567 upstream.\n" + _GOOD_CONSUMER
        )
        self.assertIsNone(cited)
        self.assertTrue(any("names no commit" in p for p in problems))

    def test_naming_the_phrase_in_a_code_span_does_not_satisfy_it(self):
        """#1057's defeat shape, applied to the gate."""
        problems, _ = gate.check_section(
            "see that entry's own `Devhub validation:` note\n" + _GOOD_CONSUMER
        )
        self.assertTrue(any("no 'Devhub validation:'" in p for p in problems))


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


class TestCodeIdentity(unittest.TestCase):
    """Against a throwaway repository, so the result cannot depend on the
    clone depth CI happens to use and nothing outside the temp dir moves."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        (self.repo / "custom_components").mkdir()
        (self.repo / "custom_components" / "a.py").write_text("x = 1\n")
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "validated")
        self.validated = _git(self.repo, "rev-parse", "HEAD")
        self._orig_root = gate.REPO_ROOT
        gate.REPO_ROOT = self.repo

    def tearDown(self):
        gate.REPO_ROOT = self._orig_root
        self._tmp.cleanup()

    def test_a_changelog_only_commit_after_validation_passes(self):
        (self.repo / "CHANGELOG.md").write_text("# Changelog\nDevhub validation: ...\n")
        _git(self.repo, "commit", "-qam", "record the verdict")
        self.assertEqual(gate.code_differs(self.validated[:7], "HEAD"), [])

    def test_a_code_change_after_validation_is_caught(self):
        (self.repo / "custom_components" / "a.py").write_text("x = 2\n")
        _git(self.repo, "commit", "-qam", "a fix pushed after devhub was checked")
        self.assertEqual(
            gate.code_differs(self.validated[:7], "HEAD"), ["custom_components/a.py"]
        )

    def test_an_unknown_citation_is_reported_not_crashed_on(self):
        self.assertIsNone(gate.code_differs("deadbee", "HEAD"))

    def test_main_end_to_end(self):
        """The full gate on a tagged commit, both directions."""
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.0.0] - 2026-09-29\n\n"
            f"- Devhub validation: installed at {self.validated[:7]}, restarted.\n"
            + _GOOD_CONSUMER
        )
        _git(self.repo, "commit", "-qam", "verdict")
        _git(self.repo, "tag", "v1.0.0")
        gate.CHANGELOG = self.repo / "CHANGELOG.md"
        try:
            self.assertEqual(gate.main(["v1.0.0"]), 0)
            (self.repo / "custom_components" / "a.py").write_text("x = 3\n")
            _git(self.repo, "commit", "-qam", "late change")
            _git(self.repo, "tag", "-f", "v1.0.0")
            self.assertEqual(gate.main(["v1.0.0"]), 1)
        finally:
            gate.CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

    def test_the_tags_changelog_is_judged_not_the_working_trees(self):
        """Found by a real local pre-check of v0.94.430 run from a branch that
        lacked the verdict: the gate read the working tree and refused a tag
        that carried one. Both directions must follow the TAG."""
        verdict = (
            "# Changelog\n\n## [1.0.0] - 2026-09-29\n\n"
            f"- Devhub validation: installed at {self.validated[:7]}, restarted.\n"
            + _GOOD_CONSUMER
        )
        bare = "# Changelog\n\n## [1.0.0] - 2026-09-29\n\n- Fixed a thing.\n"
        gate.CHANGELOG = self.repo / "CHANGELOG.md"
        try:
            # Tag carries the verdict; working tree does not -> must PASS.
            (self.repo / "CHANGELOG.md").write_text(verdict)
            _git(self.repo, "commit", "-qam", "verdict")
            _git(self.repo, "tag", "v1.0.0")
            (self.repo / "CHANGELOG.md").write_text(bare)
            self.assertEqual(gate.main(["v1.0.0"]), 0)
            # Tag lacks the verdict; working tree has it -> must FAIL.
            _git(self.repo, "commit", "-qam", "drop verdict")
            _git(self.repo, "tag", "-f", "v1.0.0")
            (self.repo / "CHANGELOG.md").write_text(verdict)
            self.assertEqual(gate.main(["v1.0.0"]), 1)
        finally:
            gate.CHANGELOG = _REPO_ROOT / "CHANGELOG.md"


class TestReleaseWorkflowRunsTheGate(unittest.TestCase):
    def test_gate_runs_before_the_release_is_published(self):
        text = _RELEASE_YML.read_text(encoding="utf-8")
        gate_at = text.find("check_release_validated.py")
        publish_at = text.find("action-gh-release")
        self.assertGreater(gate_at, 0, "release.yml no longer runs the #1447 gate")
        self.assertLess(gate_at, publish_at, "the gate must run before publishing")

    def test_checkout_keeps_full_history(self):
        """A cited commit is only resolvable in a full clone."""
        self.assertRegex(_RELEASE_YML.read_text(encoding="utf-8"), r"fetch-depth:\s*0")


if __name__ == "__main__":
    unittest.main()
