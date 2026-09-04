"""Regression test for nimbus issue #358 (Mark Purcell, codebase review):
every `uses:` action reference across this repo's GitHub Actions
workflows used to be pinned to a mutable tag or branch (`@v5`, `@master`,
`@main`) rather than a commit SHA -- a floating reference can start
running different, unreviewed code on this repo's own default
GITHUB_TOKEN at any time, with no diff to review.

Every `uses:` is now pinned to a full 40-character commit SHA, with a
trailing `# vX.Y.Z` (or `# master @ <date>` for the two actions with no
real semver tag) comment so a human can see what it corresponds to.

This test scans the raw workflow file text directly (no YAML parser
dependency needed) -- a plain regex is sufficient and keeps this test
free of any new project dependency.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Matches `uses: owner/repo[/subpath]@ref` -- captures the ref (whatever
# comes after `@`, up to whitespace).
_USES_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)@(\S+)", re.MULTILINE)
_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class TestEveryWorkflowActionIsPinnedToACommitSha(unittest.TestCase):
    def _workflow_files(self) -> list[Path]:
        files = sorted(_WORKFLOWS_DIR.glob("*.yml"))
        self.assertTrue(files, f"no workflow files found under {_WORKFLOWS_DIR}")
        return files

    def test_every_uses_reference_is_a_full_commit_sha(self):
        for path in self._workflow_files():
            text = path.read_text(encoding="utf-8")
            matches = _USES_PATTERN.findall(text)
            self.assertTrue(
                matches, f"{path.name}: no `uses:` lines found -- test setup issue"
            )
            for action, ref in matches:
                with self.subTest(file=path.name, action=action, ref=ref):
                    self.assertRegex(
                        ref,
                        _FULL_SHA_PATTERN,
                        f"{path.name}: {action}@{ref} is not pinned to a full "
                        "40-character commit SHA -- a mutable tag/branch "
                        "reference (@v5, @master, @main, etc.) can silently "
                        "change what code runs against this repo's own "
                        "GITHUB_TOKEN with no diff to review",
                    )

    def test_every_pinned_action_has_a_version_comment(self):
        # A bare SHA with no trailing comment is technically pinned but
        # tells a future maintainer (or Dependabot/Renovate) nothing
        # about what it corresponds to or how to bump it deliberately.
        for path in self._workflow_files():
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "uses:" not in line or "@" not in line:
                    continue
                with self.subTest(file=path.name, line=line.strip()):
                    self.assertIn(
                        "#",
                        line,
                        f"{path.name}: {line.strip()!r} is pinned to a SHA but "
                        "has no trailing version comment",
                    )
