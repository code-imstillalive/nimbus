#!/usr/bin/env python3
"""Refuse to publish a release whose CHANGELOG entry does not yet carry
its validation verdict -- nimbus issue #1447.

## The failure this closes

Twice in the #1298 decomposition a release was tagged BEFORE it was
validated, and twice a "confirmed on devhub" line was written before the
check it described had run. v0.94.428 was the expensive one: tagged,
published, offered by HACS, and unable to solve at all on a real install.

The stated fix after each incident was a discipline -- "the verification
line gets written after the verification, never in the edit that sets
up the release". Nothing enforced it, and nothing could have from the
existing guard: `tests/test_changelog_release_validation.py` (#594)
deliberately exempts the in-flight version, because the line is written
after the release is cut. That exemption is correct for CI on `main`,
and it is exactly the window a premature tag falls through.

## Why the gate lives in release.yml

A pushed tag is not what an install sees. HACS tracks GitHub *Releases*
(see CLAUDE.md, "devhub (and any other HACS install) needs a cut
release"), and `release.yml` is the only thing that publishes one. So a
release step that refuses to publish is a structural stop: the tag can
exist, but no install is offered it until the tagged commit's own
CHANGELOG section states what was validated.

Because the tag must point at a commit that already contains the
verdict, the order becomes mechanical rather than remembered:

    1. install `main` on devhub (HACS can install the default branch)
    2. validate it
    3. commit the "Devhub validation:" line, citing the commit validated
    4. tag THAT commit

Tagging first no longer produces a release anyone can install.

## The commit citation

A line that exists proves only that someone wrote it. If the validation
line cites the commit that was validated -- `at 9d3be28`, or any 7-40
character hex hash after "at"/"commit" -- this script checks that
`custom_components/` is byte-identical between that commit and the tag.
That turns "the tagged code is the code that was validated" from a claim
into a check, and it is what catches the real hazard: a fix pushed
between the validation and the tag.

A citation is required unless the line says "not claimed". "Devhub
validation: not claimed, because X" is a first-class answer under the
RELEASE VALIDATION directive -- a tests-only release, a path the install
has no load for -- and this gate must never make the honest answer
harder to give than a false one.

## Regexes are shared, not copied

The validation and consumer patterns, and the prose-only stripping, are
the #594 guard's own. `tests/test_release_gate_matches_changelog_guard.py`
asserts they are identical, so the gate cannot drift into accepting a
line CI would reject or vice versa.

Usage:
    python .github/scripts/check_release_validated.py v0.94.430
    python .github/scripts/check_release_validated.py v0.94.430 --tag-ref v0.94.430
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CODE_DIR = "custom_components/"

# --- Shared with tests/test_changelog_release_validation.py (#594). ----
# Kept identical by tests/test_release_gate_matches_changelog_guard.py;
# see that guard for why each part is shaped the way it is.
VALIDATION_RE = re.compile(
    r"^\s*(?:-\s*)?devhub validation:", re.IGNORECASE | re.MULTILINE
)
_CONSUMER_PLACEHOLDER = r"n/?a|tbd|tba|todo|none|unknown|nil|\?+|-+|\.+"
CONSUMER_RE = re.compile(
    r"^[ \t]*(?:-[ \t]*)?consumer check:[ \t]*"
    r"(?!(?:" + _CONSUMER_PLACEHOLDER + r")[ \t]*$)"
    r"(?=[^\n]*[A-Za-z]{3})"
    r"[^\n]{15,}",
    re.IGNORECASE | re.MULTILINE,
)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def prose_only(body: str) -> str:
    """The entry with code spans and fenced blocks removed, so that
    naming the phrase cannot be mistaken for stating it."""
    return _CODE_SPAN_RE.sub(" ", _FENCED_BLOCK_RE.sub(" ", body))


# --- This gate's own additions. ----------------------------------------
# The whole validation line: from the phrase to the end of its paragraph
# (a blank line, the next bullet, or the next heading), since the real
# file wraps these lines across several physical lines.
_VALIDATION_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?devhub validation:(.*?)(?=\n\s*\n|\n\s*-\s|\n#|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_CITED_COMMIT_RE = re.compile(r"\b(?:at|commit)\s+\**([0-9a-f]{7,40})\b", re.IGNORECASE)
_NOT_CLAIMED_RE = re.compile(r"\bnot claimed\b", re.IGNORECASE)


def section_for(version: str, text: str) -> str | None:
    """The body of `## [version]`, up to the next `## [` heading."""
    match = re.search(
        r"^## \[" + re.escape(version) + r"\][^\n]*\n(.*?)(?=^## \[|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else None


def check_section(body: str) -> tuple[list[str], str | None]:
    """Return (problems, cited_commit). Pure -- no git, so it is testable
    against a string."""
    problems: list[str] = []
    prose = prose_only(body)
    if not VALIDATION_RE.search(prose):
        problems.append(
            "no 'Devhub validation:' line. Validate on devhub first (HACS can "
            "install `main`), then write what was checked -- or 'Devhub "
            "validation: not claimed, because X' where that is the honest answer."
        )
    if not CONSUMER_RE.search(prose):
        problems.append(
            "no 'Consumer check:' line with real content. 'Nothing "
            "user-visible changed' is a real answer; a bare label is not."
        )
    cited: str | None = None
    line = _VALIDATION_LINE_RE.search(prose)
    if line:
        text = line.group(1)
        commit = _CITED_COMMIT_RE.search(text)
        if commit:
            cited = commit.group(1)
        elif not _NOT_CLAIMED_RE.search(text):
            problems.append(
                "the 'Devhub validation:' line names no commit. Cite the commit "
                "that was installed and validated ('... at 9d3be28'), so the gate "
                "can prove the tagged code is the validated code -- or say "
                "'not claimed' if no validation was possible."
            )
    return problems, cited


def code_differs(cited: str, tag_ref: str) -> list[str] | None:
    """Files under custom_components/ that differ between the validated
    commit and the tag, or None if the cited commit is not in history."""
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{cited}^{{commit}}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None
    out = subprocess.run(
        ["git", "diff", "--name-only", cited, tag_ref, "--", CODE_DIR],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.splitlines() if p.strip()]


def changelog_at(ref: str) -> str:
    """CHANGELOG.md as it exists AT `ref` -- the tag -- not in the working tree.

    In `release.yml` the checkout is the tag, so the two agree. Anywhere else
    they need not: run as a local pre-check from a branch that lacks the
    verdict, this used to report "no 'Devhub validation:' line" for a tag that
    has one, and a branch that HAS a verdict the tag lacks would pass a tag
    that should fail. Found by exactly that local run for v0.94.430. Falls back
    to the working tree only when the ref does not resolve (e.g. a tag not yet
    created), and says so.
    """
    try:
        return subprocess.run(
            ["git", "show", f"{ref}:CHANGELOG.md"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        print(
            f"note: {ref} does not resolve; reading the working tree's CHANGELOG.md",
            file=sys.stderr,
        )
        return CHANGELOG.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, e.g. v0.94.430")
    parser.add_argument(
        "--tag-ref",
        default=None,
        help="git ref the tag points at (default: the tag itself)",
    )
    args = parser.parse_args(argv)

    version = args.tag.removeprefix("v")
    body = section_for(version, changelog_at(args.tag_ref or args.tag))
    if body is None:
        print(f"::error::No '## [{version}]' section in CHANGELOG.md.", file=sys.stderr)
        return 1

    problems, cited = check_section(body)
    if cited:
        differing = code_differs(cited, args.tag_ref or args.tag)
        if differing is None:
            problems.append(
                f"the validation line cites commit {cited}, which is not in this "
                f"repository's history -- the citation cannot be checked."
            )
        elif differing:
            problems.append(
                f"custom_components/ changed between the validated commit {cited} "
                f"and {args.tag}: {', '.join(differing)}. The tagged code is not "
                f"the code that was validated -- re-validate at the tag's commit."
            )

    if problems:
        for p in problems:
            print(f"::error::{args.tag}: {p}", file=sys.stderr)
        print(
            f"Refusing to publish {args.tag} (nimbus issue #1447). The tag can "
            f"stay; no install is offered it until this passes.",
            file=sys.stderr,
        )
        return 1

    detail = f", code identical to validated commit {cited}" if cited else ""
    print(f"{args.tag}: validation and consumer lines present{detail}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
