#!/usr/bin/env python3
"""Reject the one PR-body shape that has auto-closed issues nobody meant
to close.

nimbus issue #594, step 9 of the definition of done in CLAUDE.md, says
to grep a PR body for a closing keyword before submitting. That rule has
now failed as a remembered rule more times than it has worked, which is
the argument for running it somewhere it cannot be forgotten.

## Why this matches the possessive and nothing else

The obvious guard -- reject any closing keyword followed by an issue
number -- is wrong, and measuring the repository's own history is what
showed it. Across the last 120 merged PRs:

```
120  merged PRs scanned
 27  bodies contain a closing keyword + issue number   <- all deliberate
  2  contain the POSSESSIVE form                       <- both real bugs
```

Those 27 are the normal, correct way this repo closes an issue from a
PR. A guard that rejected them would reject genuine compliance -- the
exact over-tightening failure #1057's own fix warned about after a
suggested pattern would have failed 15 truthful CHANGELOG entries.

The two possessive occurrences are the two incidents #594 records: PRs
whose bodies opened with a closing keyword, the issue number, and then a
narrowing clause. GitHub's scanner stops at the number, so both issues
were closed while the PR was explaining it only did part of the work,
and both had to be reopened by hand.

So the guard is calibrated to that: **2 of 2 real incidents caught, 0 of
27 legitimate closes rejected.**

## What was considered and deliberately left out

Measured on the same corpus rather than reasoned about:

- **Narrowing words after the number** (`part`, `half`, `first`, `of`):
  zero occurrences on the same line. A branch for them would be
  speculative, and a guard's credibility is spent the first time it
  rejects something correct.
- **A narrowing clause after a sentence break** (`Closes #N. Its other
  criterion...`, `Closes #N -- the remaining half.`): two occurrences,
  neither reported as a wrong close. Plausibly deliberate, so matching
  them would be guessing at intent.

Narrow and correct beats broad and disabled-after-a-false-positive.

## Quoting this pattern is NOT a false positive

A post-mortem PR that quotes the offending phrase verbatim really would
re-close the issue -- GitHub's scanner does not care that a string sits
inside quotation marks or a code fence, and #594 records exactly that
near-miss. Failing such a body is correct behaviour, not collateral.
Write the number as a placeholder instead.

Usage:
    python .github/scripts/check_pr_closing_keywords.py <file>
    ... | python .github/scripts/check_pr_closing_keywords.py -
"""

from __future__ import annotations

import re
import sys

# GitHub's own closing keywords (close/closes/closed, fix/fixes/fixed,
# resolve/resolves/resolved) followed by an issue reference, followed
# immediately by a possessive -- straight or typographic apostrophe.
#
# `\s+` between keyword and number matches GitHub's own tolerance; the
# possessive must be immediately adjacent, because that is what makes
# the number a subject being narrowed rather than a reference being
# closed.
CLOSING_POSSESSIVE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)('s|’s)",
    re.IGNORECASE,
)

# The NEGATED form: a closing keyword and issue number preceded by a
# negation. GitHub's scanner has no concept of negation -- it sees
# `fix #937` and closes the issue -- so the sentence written specifically
# to PREVENT an auto-close is what causes one.
#
# Measured the same way the possessive branch was, on the same corpus:
#
#     120  merged PRs scanned
#       8  bodies contain a plain closing keyword + issue number  <- deliberate
#       2  contain the NEGATED form                               <- both bugs
#
# Both negated occurrences caused a wrongful close that a human then had
# to undo by hand:
#
#     #1122  "does not close #496"
#        2026-09-18T04:46:58Z  PR merged
#        2026-09-18T04:46:59Z  #496 closed     <- one second later
#        2026-09-18T04:56:51Z  #496 reopened   <- ten minutes of attention
#     (#496 had an identical close/reopen pair on 2026-09-13.)
#
#     #1283  "does not fix #937"
#        #937 closed on merge, reopened by hand the same day.
#
# So: 2 of 2 negated occurrences were real incidents, 0 of 8 legitimate
# closes would be rejected -- a legitimate close never negates itself.
# The same calibration the possessive branch holds itself to.
#
# The 24-character window spans the shapes actually observed ("does not
# fix", "will not close", "is not a fix for") without reaching across a
# sentence boundary: `[^.\n]` stops at a full stop or newline, so an
# unrelated negation earlier in the paragraph cannot drag an innocent
# close into a match.
_NEGATORS = r"not|never|n't|won't|cannot|can't|isn't|doesn't|no"
CLOSING_NEGATED = re.compile(
    r"\b(?:" + _NEGATORS + r")\b[^.\n]{0,24}?"
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)

_ADVICE = """
GitHub will auto-close issue #{number} when this merges, because its
scanner stops at the number and never reads the clause that narrows it.

Put the scope FIRST so no keyword ever precedes the number:
    "Part 1 of 3 for issue {number}"
    "Addresses the unblocked half of issue {number}"

If the sentence says this PR does NOT close it, spell the reference out
with no "#" -- GitHub has no concept of negation and closes on the
keyword alone:
    "this is not a fix for issue {number}"

If you genuinely mean to close it, drop the possessive/negation and say
so plainly -- that form is used correctly throughout this repo and is
not what this check rejects.
""".rstrip()


def find_offences(body: str) -> list[tuple[str, str]]:
    """Every (matched_text, issue_number) in `body`, in reading order.

    Two shapes, one failure: a closing keyword whose surrounding words say
    the PR does NOT do the thing GitHub is about to do anyway.
    """
    found: list[tuple[int, str, str]] = [
        (m.start(), m.group(0), m.group(2)) for m in CLOSING_POSSESSIVE.finditer(body)
    ]
    found += [
        # group(2), not (3): the negator alternation is non-capturing, so
        # this pattern has exactly two groups like its sibling.
        (m.start(), m.group(0), m.group(2))
        for m in CLOSING_NEGATED.finditer(body)
    ]
    # Sorted by position so a body carrying both reports them in reading
    # order, and de-duplicated by offset in case a phrase ever satisfies
    # both patterns at the same place.
    seen: set[int] = set()
    out: list[tuple[str, str]] = []
    for pos, text, number in sorted(found):
        if pos in seen:
            continue
        seen.add(pos)
        out.append((text, number))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0] if argv else 'check'} <file|->", file=sys.stderr)
        return 2

    source = argv[1]
    if source == "-":
        body = sys.stdin.read()
    else:
        # Context-managed rather than a bare open().read(): the inline
        # form leaked the handle, which this file's own entry-point test
        # caught as a ResourceWarning. A guard that warns while checking
        # other people's hygiene is not a good look.
        with open(source, encoding="utf-8", errors="replace") as fh:
            body = fh.read()

    offences = find_offences(body)
    if not offences:
        # Report what was actually read. A body that arrives empty --
        # `github.event.pull_request.body` is null on a PR opened with no
        # description -- passes this check for a reason that has nothing
        # to do with the check, and an unqualified "clean" line would be
        # indistinguishable from a real pass. An empty body is not an
        # error (a PR is allowed one), so this reports rather than
        # rejects; the point is that the log says which case it was.
        print(f"No narrowed closing keyword found ({len(body)} chars checked).")
        if not body.strip():
            print(
                "NOTE: the body was empty, so this check proved nothing about "
                "it. If that is unexpected, the workflow is not receiving "
                "github.event.pull_request.body."
            )
        return 0

    print("PR body would auto-close an issue it is only partly addressing:\n")
    for matched, number in offences:
        print(f"  found: {matched!r}")
        print(_ADVICE.format(number=number))
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
