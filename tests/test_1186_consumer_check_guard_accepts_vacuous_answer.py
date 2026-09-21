"""IV&V finding (c881b523..6d3a6d0 pass, 2026-09-21): #1171's own
`_CONSUMER_RE` guard (test_changelog_release_validation.py, nimbus
issue #594 criterion 2) matches on the literal label `"consumer
check:"` alone -- nothing requires real content after the colon.

The same commit hardened `_VALIDATION_RE`'s sibling check against the
CODE-SPAN-mention defeat (#1057) via `_prose_only()`, but
`_CONSUMER_RE` has no equivalent defence against the EMPTY-ANSWER
defeat: a bare `"Consumer check:"` or a placeholder like `"Consumer
check: N/A"` satisfies the regex with zero substantive content,
exactly the "guard that cannot fail is indistinguishable from one that
works" failure this repo's own CLAUDE.md names repeatedly.

Worth being precise about what should still pass: this repo's own
convention treats "nothing user-visible changed" as a legitimate,
honest answer to the consumer-check question (the guard's own failure
message says so explicitly) -- so the fix must not require every entry
to describe a visible change, only that the label is followed by real
content rather than a bare colon or a placeholder.

This test imports the guard's own real regex and prose-stripping
helper directly from the source test file (not a reimplementation) and
drives them against the two vacuous shapes.
"""

from __future__ import annotations

import unittest

from test_changelog_release_validation import _CONSUMER_RE, _prose_only


class TestTheGuardRejectsAVacuousAnswer(unittest.TestCase):
    # All three were xfail(strict=True) when Mark filed this. Closed in
    # the same pass: _CONSUMER_RE now requires real content after the
    # colon -- a placeholder lookahead, a real-word lookahead and a
    # length floor. "Nothing user-visible changed" still passes.
    def test_a_bare_label_does_not_satisfy_the_guard(self):
        self.assertIsNone(
            _CONSUMER_RE.search(_prose_only("Consumer check:")),
            "a CHANGELOG entry with only the bare 'Consumer check:' "
            "label and nothing after it passed the guard meant to "
            "confirm the consumer question was actually answered",
        )

    def test_a_placeholder_answer_of_na_does_not_satisfy_the_guard(self):
        self.assertIsNone(
            _CONSUMER_RE.search(_prose_only("Consumer check: N/A")),
            "'Consumer check: N/A' passed the guard with a placeholder "
            "answer that names no real content",
        )

    def test_a_placeholder_answer_of_tbd_does_not_satisfy_the_guard(self):
        self.assertIsNone(
            _CONSUMER_RE.search(_prose_only("Consumer check: tbd")),
            "'Consumer check: tbd' passed the guard with a placeholder "
            "answer that names no real content",
        )


if __name__ == "__main__":
    unittest.main()
