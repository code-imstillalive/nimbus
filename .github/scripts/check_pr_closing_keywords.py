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

_ADVICE = """
GitHub will auto-close issue #{number} when this merges, because its
scanner stops at the number and never reads the clause that narrows it.

Put the scope FIRST so no keyword ever precedes the number:
    "Part 1 of 3 for issue {number}"
    "Addresses the unblocked half of issue {number}"

If you genuinely mean to close it, drop the possessive and say so
plainly -- that form is used correctly throughout this repo and is not
what this check rejects.
""".rstrip()


def find_offences(body: str) -> list[tuple[str, str]]:
    """Every (matched_text, issue_number) in `body`, in order."""
    return [(m.group(0), m.group(2)) for m in CLOSING_POSSESSIVE.finditer(body)]


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
        print("No narrowed closing keyword found.")
        return 0

    print("PR body would auto-close an issue it is only partly addressing:\n")
    for matched, number in offences:
        print(f"  found: {matched!r}")
        print(_ADVICE.format(number=number))
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
