#!/usr/bin/env python3
"""Fail a PR that deletes a file its body does not name -- nimbus issue #1445.

## The incident, reconstructed from git rather than recalled

PR #1384 (commit 30116ff) migrated the golden snapshots from `.json.gz`
to plain `.json`, and in doing so deleted `docs/specs/003-*.md` and
`docs/specs/004-*.md` -- two approved, merged specs nobody meant to touch.
They were restored by hand in #1393.

The mechanism is worth stating exactly, because it decides what a check
has to look at. The PR's single commit `70b97ac` has parent `f8d99f0`,
which IS the actual merge-base with `main` and DOES contain both specs.
A `git reset --soft` moved HEAD onto a fresh ref while keeping an index
built from an older one, so the commit's tree simply lacked the specs.

**So the deletion was visible** in the PR's own diff against its actual
merge-base -- as two lines among sixteen deletions, fourteen of them the
intended `.json.gz` migration. That rules out the obvious check ("flag a
deletion the PR's diff did not make"): the PR's diff did make it. What
was missing was not visibility but intent, and no diff can recover
intent. Only the author can state it.

## So: every deleted path must be named in the PR body

A line of the form

    Deletes: tests/golden/snapshots/*.json.gz

(bullet optional, paths or globs, comma- or space-separated, code spans
fine) acknowledges each deletion it matches. A deletion no line matches
fails the check, naming the path.

The #1384 author would have written the `.json.gz` line -- the migration
was the point of the PR -- and would never have listed specs they did
not know were gone. That is the whole mechanism.

## The cost, measured before choosing it

Of the last 300 changes merged to `main`, **2** deleted any file at all:
#1384 (the incident itself) and ec7e6af (one deliberate file). A rule
that asks for one extra line on under 1% of PRs, and would have stopped
the one real incident in that window, is cheap. Renames are detected
(`-M`) and are not deletions, so a move does not need listing.

Usage (CI):
    python .github/scripts/check_pr_deletions.py --base <sha> --head <sha> --body-file -
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_ACK_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?deletes:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE
)


def acknowledged_patterns(body: str) -> list[str]:
    """Every path or glob named on a `Deletes:` line."""
    patterns: list[str] = []
    for match in _ACK_RE.finditer(body or ""):
        for token in re.split(r"[,\s]+", match.group(1).replace("`", " ")):
            token = token.strip().strip(".;")
            if token:
                patterns.append(token)
    return patterns


def unacknowledged(deleted: list[str], patterns: list[str]) -> list[str]:
    """Deleted paths no pattern matches. A bare directory also covers
    everything beneath it."""
    out = []
    for path in deleted:
        if not any(
            fnmatch.fnmatchcase(path, p) or path.startswith(p.rstrip("/") + "/")
            for p in patterns
        ):
            out.append(path)
    return out


def deleted_files(base: str, head: str, repo: Path = REPO_ROOT) -> list[str]:
    """Files the PR deletes relative to its ACTUAL merge-base with the
    base branch -- never the base tip, which would count every file
    added on main since as a deletion."""
    merge_base = subprocess.run(
        ["git", "merge-base", base, head],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    out = subprocess.run(
        ["git", "diff", "-M", "--name-only", "--diff-filter=D", merge_base, head],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.splitlines() if p.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--body-file", required=True, help="PR body path, or - for stdin"
    )
    args = parser.parse_args(argv)

    body = (
        sys.stdin.read()
        if args.body_file == "-"
        else Path(args.body_file).read_text(encoding="utf-8")
    )
    deleted = deleted_files(args.base, args.head)
    missing = unacknowledged(deleted, acknowledged_patterns(body))
    if missing:
        for path in missing:
            print(
                f"::error::this PR deletes {path}, and its body does not say so.",
                file=sys.stderr,
            )
        print(
            "\nEvery deleted file must be named on a 'Deletes:' line in the PR body "
            "(paths or globs), e.g.\n\n    Deletes: tests/golden/snapshots/*.json.gz\n\n"
            "If you did not mean to delete it, this is nimbus issue #1445 happening "
            "again -- a squash or reset built from a stale index. Restore the file "
            "rather than listing it.",
            file=sys.stderr,
        )
        return 1
    print(f"{len(deleted)} deleted file(s), all named in the PR body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
