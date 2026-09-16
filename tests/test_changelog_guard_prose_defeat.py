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

xfail(strict=True): pins the gap. The fix shape suggested in the filed
issue is requiring the phrase to open a bullet/line (e.g. anchored at
the start of a markdown list item: `^\\s*-\\s*devhub validation:`)
rather than matching anywhere in prose -- at which point this test
should XPASS and the marker should come off.
"""

from __future__ import annotations

import unittest

import pytest
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


@pytest.mark.xfail(
    reason=(
        "IV&V (since-#996 pass, 2026-09-17): the #594 criterion-1 guard's "
        "phrase search matches prose that discusses a PAST release's "
        "validation line, not just prose asserting the CURRENT release's "
        "own -- see this file's own module docstring for the constructed "
        "repro. Fix: anchor the regex to a bullet/line start instead of "
        "matching anywhere in the section body."
    ),
    strict=True,
)
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


if __name__ == "__main__":
    unittest.main()
