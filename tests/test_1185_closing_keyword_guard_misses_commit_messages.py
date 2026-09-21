"""IV&V finding (c881b523..6d3a6d0 pass, 2026-09-21): #1158's own
PR-body closing-keyword guard (`.github/scripts/check_pr_closing_keywords.py`,
wired into `.github/workflows/ci.yml`'s `pr-body-hygiene` job) only ever
receives `github.event.pull_request.body`. GitHub's own issue-closing
scanner does not only read the PR description -- it also reads the
commit message that lands on the default branch.

This repo's own history demonstrates the exact mechanism: its
squash-merge commits are the verbatim concatenation of every
constituent commit's own message (confirmed directly against commit
`3a9ddca`, v0.94.396/PR #1144, whose final message on `main` is
literally `"* <commit 1 message>"` + `"* <commit 2 message>"` separated
by `---------` -- GitHub's default, unedited squash-message pre-fill).

So a PR whose final body is clean (passes this guard) but whose early,
since-superseded commit message once carried a closing-keyword-plus-
possessive phrase (e.g. "Closes #1120's first half", later rewritten
in the body but never amended in that commit) still auto-closes the
issue on merge, because the squash commit message GitHub's scanner
reads is built from the commit history this guard never looks at.

This test parses the real CI workflow file and pins that the
`pr-body-hygiene` job's check currently covers only the PR body, never
any commit-message content -- the structural gap, not a regex defect
(`find_offences()`'s own matching logic is correct and out of scope
here; see the existing `tests/test_pr_closing_keyword_guard.py`).
"""

from __future__ import annotations

import pathlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
