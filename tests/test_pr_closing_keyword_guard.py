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


class TestTheNegatedFormIsRejected(unittest.TestCase):
    """The second shape, and the one that bit twice before it was guarded.

    GitHub's scanner has no concept of negation: it sees `fix #937` and
    closes the issue. So a sentence written specifically to PREVENT an
    auto-close is what causes one.

    Both quoted bodies are real and both produced a wrongful close a human
    then undid by hand:

        #1122  "does not close #496"
           2026-09-18T04:46:58Z  PR merged
           2026-09-18T04:46:59Z  #496 closed     <- one second later
           2026-09-18T04:56:51Z  #496 reopened   <- ten minutes of attention
        (#496 had an identical close/reopen pair on 2026-09-13.)

        #1283  "does not fix #937"
           #937 closed on merge, reopened by hand the same day.
    """

    def test_the_1283_incident_is_caught(self):
        body = "**This does not fix #937.** It names the blocker precisely."
        self.assertEqual(
            [n for _, n in guard.find_offences(body)],
            ["937"],
        )

    def test_the_1122_incident_is_caught(self):
        body = "This does not close #496 -- the flex family only."
        self.assertEqual(
            [n for _, n in guard.find_offences(body)],
            ["496"],
        )

    def test_other_real_negator_spellings(self):
        for body in (
            "This PR will not close #123 yet.",
            "It cannot fix #123 on its own.",
            "This never closes #123.",
            "It doesn't resolve #123.",
        ):
            with self.subTest(body=body):
                self.assertTrue(guard.find_offences(body), body)


class TestTheNegatedBranchDoesNotOverreach(unittest.TestCase):
    """Calibrated to the same bar the possessive branch set: 2 of 2 real
    incidents caught, 0 of 8 legitimate closes rejected. A legitimate close
    never negates itself, which is what makes that possible."""

    def test_a_negation_in_a_PRIOR_sentence_is_not_an_offence(self):
        """The window stops at a full stop, so an unrelated negation earlier
        in the paragraph cannot drag an innocent close into a match."""
        self.assertEqual(guard.find_offences("No new tests needed. Closes #77"), [])

    def test_a_negation_on_a_PREVIOUS_line_is_not_an_offence(self):
        self.assertEqual(guard.find_offences("Not a refactor\nCloses #77"), [])

    def test_the_recommended_safe_phrasing_passes(self):
        """What the advice text tells people to write instead. If this ever
        failed, the guard would be rejecting its own remedy."""
        self.assertEqual(
            guard.find_offences("this is not a fix for issue 937"),
            [],
        )

    def test_keyword_separated_from_the_number_is_not_flagged(self):
        """ "a fix for #55" does NOT auto-close on GitHub -- the keyword must
        be immediately followed by the reference. Flagging it would reject
        something harmless, so the guard deliberately mirrors GitHub's own
        parser rather than being broader than it.

        Recorded because my first draft of this test asserted the opposite
        and the implementation was right.
        """
        self.assertEqual(guard.find_offences("It isn't a fix for #55"), [])

    def test_plain_closes_still_pass(self):
        for body in ("Closes #123", "Fixes #123 and #124", "Resolves #9"):
            with self.subTest(body=body):
                self.assertEqual(guard.find_offences(body), [])


class TestBothShapesReportTogether(unittest.TestCase):
    def test_a_body_carrying_both_reports_both_in_reading_order(self):
        body = "Fixes #100's own first half. Also, this does not fix #200."
        self.assertEqual([n for _, n in guard.find_offences(body)], ["100", "200"])

    def test_no_duplicate_report_for_one_position(self):
        """De-duplicated by offset, so a phrase satisfying both patterns is
        reported once rather than twice."""
        offences = guard.find_offences("does not fix #937")
        self.assertEqual(len(offences), 1)
