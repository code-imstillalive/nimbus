"""Tests for `.github/scripts/check_pr_closing_keywords.py`.

nimbus issue #594's own record is that this repo has now shipped three
guards that looked like they worked and did not, plus one near-miss in
the opposite direction (a suggested pattern that would have failed 15
truthful entries). Both directions are live failure modes, and the
over-tight one is easier to ship accidentally because it fails loudly in
CI and looks like it is working.

So this file asserts both directions, against the real corpus rather
than only against constructed strings:

- the two historical bodies that genuinely auto-closed an issue nobody
  meant to close must be REJECTED
- the ordinary closing forms used correctly in 27 of the last 120 merged
  PRs must be ACCEPTED
- the two near-shapes that were measured and deliberately left out must
  be ACCEPTED, so a later widening is a deliberate choice rather than a
  silent one

The literal pattern appears in this file, which is safe: GitHub's
scanner reads PR bodies and commit messages, not file contents.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "scripts"
    / "check_pr_closing_keywords.py"
)
_spec = importlib.util.spec_from_file_location("check_pr_closing_keywords", _SCRIPT)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_pr_closing_keywords"] = guard
_spec.loader.exec_module(guard)


class TestTheTwoRealIncidentsAreRejected(unittest.TestCase):
    """Both are quoted from the PRs that actually closed #496 and #1120
    while saying, in the same sentence, that they only did part of the
    work. Both issues had to be reopened by hand."""

    def test_the_partial_scope_possessive_is_caught(self):
        body = "Closes #1120's first of three parts -- the version stamp."
        found = guard.find_offences(body)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "1120")

    def test_the_other_real_incident_is_caught(self):
        body = "Closes #496's **unblocked** diagnostics half."
        found = guard.find_offences(body)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "496")

    def test_a_typographic_apostrophe_is_caught_too(self):
        # A body pasted from anywhere that does smart quotes would
        # otherwise sail through while behaving identically on GitHub.
        body = "Closes #1120’s first of three parts."
        self.assertEqual(len(guard.find_offences(body)), 1)

    def test_every_closing_keyword_spelling_is_covered(self):
        for kw in (
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ):
            with self.subTest(keyword=kw):
                self.assertEqual(
                    len(guard.find_offences(f"{kw} #42's second half")),
                    1,
                    f"{kw!r} is one of GitHub's own closing keywords and "
                    "must be matched, or the guard has a hole exactly the "
                    "size of one synonym",
                )


class TestLegitimateClosesAreAccepted(unittest.TestCase):
    """27 of the last 120 merged PRs close an issue this way, correctly.
    Rejecting them would be the over-tightening failure #1057 warned
    about -- a guard that rejects real compliance is worse than the gap
    it closes."""

    def test_a_plain_close_is_not_an_offence(self):
        self.assertEqual(guard.find_offences("Closes #1140"), [])

    def test_two_plain_closes_on_their_own_lines(self):
        body = "Scope: two IV&V findings.\n\nCloses #1140\nCloses #1141\n"
        self.assertEqual(guard.find_offences(body), [])

    def test_a_close_followed_by_a_colon_and_prose(self):
        body = "Closes #1104 + #1102: close two IV&V findings."
        self.assertEqual(guard.find_offences(body), [])

    def test_a_close_followed_by_a_full_stop_and_more_prose(self):
        body = "Closes #1098. The defect, measured on a real fleet, was ..."
        self.assertEqual(guard.find_offences(body), [])

    def test_scope_first_phrasing_is_not_an_offence(self):
        # The form CLAUDE.md step 9 recommends, and the one this guard
        # exists to push people toward.
        body = "Part 1 of 3 for issue 1120 -- stamp each scored day."
        self.assertEqual(guard.find_offences(body), [])

    def test_an_unrelated_possessive_is_not_an_offence(self):
        body = "This follows @purcell-lab's filing and #1120's own thread."
        self.assertEqual(guard.find_offences(body), [])


class TestTheDeliberatelyExcludedShapes(unittest.TestCase):
    """Measured on the same corpus and left out on purpose. Pinned so
    that widening the guard later is a deliberate decision with a test
    to change, not something that drifts in."""

    def test_a_narrowing_clause_after_a_sentence_break_is_allowed(self):
        body = "Closes #1089. Its other criterion stays open."
        self.assertEqual(guard.find_offences(body), [])

    def test_a_narrowing_clause_after_a_dash_is_allowed(self):
        body = "Closes #1073 -- the remaining half."
        self.assertEqual(guard.find_offences(body), [])

    def test_a_narrowing_word_on_the_next_line_is_allowed(self):
        body = "Closes #769\n\nFirst of two parts."
        self.assertEqual(guard.find_offences(body), [])


class TestTheEntryPoint(unittest.TestCase):
    def test_a_clean_body_exits_zero(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Scope: docs only.\n\nCloses #1\n")
            path = fh.name
        self.assertEqual(guard.main(["check", path]), 0)

    def test_an_offending_body_exits_nonzero(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Closes #7's first half.\n")
            path = fh.name
        self.assertEqual(guard.main(["check", path]), 1)

    def test_wrong_arity_is_a_usage_error_not_a_pass(self):
        # Exit 2, not 0 -- a guard invoked wrongly must not look clean.
        self.assertEqual(guard.main(["check"]), 2)

    def test_an_empty_body_passes_but_says_it_proved_nothing(self):
        """`github.event.pull_request.body` is null on a PR opened with
        no description, and an unqualified "clean" line would then be
        indistinguishable from a real pass -- the vacuity failure this
        repo keeps recording. An empty body is legal, so it passes; what
        must not happen is passing silently."""
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("")
            path = fh.name

        out = io.StringIO()
        with redirect_stdout(out):
            rc = guard.main(["check", path])

        self.assertEqual(rc, 0, "an empty body is legal and must not fail")
        self.assertIn("proved nothing", out.getvalue())

    def test_a_real_body_reports_how_much_it_checked(self):
        import io
        import tempfile
        from contextlib import redirect_stdout

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Scope: docs only.\n\nCloses #1\n")
            path = fh.name

        out = io.StringIO()
        with redirect_stdout(out):
            guard.main(["check", path])

        text = out.getvalue()
        self.assertIn("chars checked", text)
        self.assertNotIn("proved nothing", text)


if __name__ == "__main__":
    unittest.main()
