"""IV&V finding (since-#996 pass, 2026-09-17): the #594 criterion-1 guard
(`tests/test_changelog_release_validation.py`) can still be satisfied by
prose that merely NAMES another release's validation, rather than
stating this release's own.

That guard already strips inline code spans and fenced blocks before
searching for "devhub validation:" -- added specifically because a
prior incident (v0.94.332) showed the phrase appearing as CODE (a
literal string reference) could falsely satisfy a bare substring
search. This test shows the mirror case still gets through: the phrase
appearing as PLAIN PROSE, but talking ABOUT a past release's line
rather than asserting one for the current release, satisfies the exact
same regex the code-span fix was meant to close off.

This is not a finding against any of the reviewed commits directly --
c56086a (#1050) touches this area (adds two *new*, similarly
unenforced, criteria) but does not introduce this specific gap; it
predates this pass. Filed because it is real, demonstrated (not
hypothetical), and exactly the "guard that looks like it works but
doesn't" pattern this repo has already hit twice before on this same
file (see that file's own module docstring).

FIXED in v0.94.366 by anchoring `_VALIDATION_RE` to the start of a line
(`re.MULTILINE`) instead of matching anywhere in the section body, and
the original xfail(strict=True) marker removed.

**One deliberate departure from the fix shape #1057 suggested**, made
after measuring the real file rather than reasoning about it: the
suggested `^\\s*-\\s*devhub validation:` requires a markdown bullet, but
CHANGELOG.md writes the line BOTH ways -- 19 entries as a `-` bullet
and 15 as an indented paragraph with no bullet (v0.94.365's own entry
among them). The dash-required form would have failed 15 truthful,
already-validated entries, so the bullet is optional. A guard that
rejects real validation lines is a worse failure than the gap it
closes, and the anchoring -- which is what actually defeats the prose
case -- is unaffected by making the dash optional.

`test_both_real_line_forms_still_match` below pins that, so a later
"simplification" to the bullet-only form fails loudly instead of
quietly invalidating a third of the file's entries.
"""

from __future__ import annotations

import unittest

from test_changelog_release_validation import _VALIDATION_RE, _prose_only

# A constructed CHANGELOG section body for some new release. No code
# spans, no fenced blocks -- ordinary prose that discusses a PAST
# release's own validation line, and asserts nothing about whether
# *this* release was checked on a real install at all.
_PROSE_THAT_MERELY_DISCUSSES_ANOTHER_RELEASE = """
### Changed
- Renamed an internal helper, no behaviour change. This release carries
  no validation of its own beyond what v0.94.332 already established --
  see that entry's own Devhub validation: note for the reasoning on why
  a rename like this one needs no fresh check.
"""


class TestTheGuardDoesNotAcceptMerelyDiscussingAnotherRelease(unittest.TestCase):
    def test_prose_about_a_past_releases_validation_is_not_this_ones(self):
        matched = bool(
            _VALIDATION_RE.search(
                _prose_only(_PROSE_THAT_MERELY_DISCUSSES_ANOTHER_RELEASE)
            )
        )
        self.assertFalse(
            matched,
            "the guard's own regex matched a section that only DISCUSSES "
            "a different release's validation line, in plain prose with "
            "no code span -- it would pass CI while stating nothing true "
            "about the release it is nominally guarding.",
        )

    def test_both_real_line_forms_still_match(self):
        """The other half of the fix, and the reason the bullet is
        optional. CHANGELOG.md writes this line two ways and both are
        truthful; a guard that only accepts one silently invalidates a
        third of the file. Measured on the real file when #1057 was
        fixed: 19 bulleted, 15 indented-paragraph.
        """
        for form in (
            "- Devhub validation: deployed, restarted, solve_now optimal.",
            "  Devhub validation: **not claimed** -- nothing to observe.",
        ):
            with self.subTest(form=form):
                self.assertTrue(
                    _VALIDATION_RE.search(_prose_only(f"### Changed\n{form}\n")),
                    f"the guard stopped recognising a real form: {form!r}",
                )

    def test_the_real_changelog_still_satisfies_the_anchored_guard(self):
        """End-to-end rather than on constructed strings: the anchoring
        must not have broken the file it guards. This is the check that
        caught the suggested bullet-only shape being too strict.
        """
        from test_changelog_release_validation import _sections

        matched = [
            v
            for v, body in _sections().items()
            if _VALIDATION_RE.search(_prose_only(body))
        ]
        self.assertGreater(
            len(matched),
            25,
            "the anchored guard recognises far fewer entries than the file "
            "actually carries -- the anchor is rejecting a real line form.",
        )


if __name__ == "__main__":
    unittest.main()
