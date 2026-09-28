"""Gate: fails when shipped code has piled up on `main` without a release
being cut, and reports the two related states that are not drift but are
routinely mistaken for it.

## Why this exists

`CLAUDE.md`'s Deploy section has said for a month that a merge is invisible
to every HACS install until a release is actually tagged: *"After merging
PR(s) to main, always follow up ... bump `manifest.json`'s `version` ...
then tag that commit `vX.Y.Z` and push the tag. Only then will HACS's update
entity on any install see the new version and offer it."*

It was not followed between v0.94.426 and v0.94.427: forty merged PRs, eleven
of them touching shipped code, and three separate issues (#1360, #768, #1357)
each blocked on the same single untaken action. #1361's diagnostics and
#1363's reconstruction both merged AFTER production had already deployed, so
`git merge-base --is-ancestor` was false for both against the tag that install
was running.

That rule had been written down, in capitals, for weeks. Writing it again is
not a fix. This is the same rule expressed as an exit code.

## The threshold, measured rather than chosen

Commits touching `custom_components/` between consecutive tags, over the 30
most recent intervals, newest first:

    17 10 9 2 6 3 6 3 4 3 2 3 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 2 2

Median 1-2. The three outliers are the three most recent intervals, which is
the degradation this gate exists to catch. **A default of 8 fires on exactly
those three and stays silent for the other 27** -- it flags the real incident
without crying wolf on the ordinary merge-then-release rhythm this repo has
used 450+ times.

Raise it deliberately via `--max-commits` if the cadence legitimately
changes, and move this paragraph's numbers when you do. A threshold whose
derivation is not written down becomes folklore within a week.

## Two states that are NOT drift, reported separately

Both are real failure modes this repo has hit, and both would be
mis-diagnosed as "needs a release" if lumped in with the commit count:

- **`manifest.json` ahead of the newest tag.** A release PR merged and the
  tag was never pushed. The work is on `main`, the version claims to be
  released, and no install can see it. This session hit the same wall from
  the other side: a tag push rejected with HTTP 403, leaving `main` bumped
  and untagged until someone with the right credentials finished it.
- **`manifest.json` behind the newest tag.** Normal only in a checkout that
  has not pulled; on `main` it means a tag was pushed at a commit whose
  manifest did not carry that version, which makes the tag a lie about its
  own contents.

## On `--report`

`--report` prints and always exits 0. **It is a display, not a check** --
the SessionStart hook uses it so a session opens knowing the state instead
of reading a stale handover file for it. Do not wire `--report` into CI and
call the repo gated; the enforcing mode is the default one.

Conversely the enforcing mode **fails when it cannot measure**, rather than
passing. A shallow clone with no tags fetched cannot answer the question, and
answering "fine" would be the #1354 shape (a gate reporting PASS having
measured nothing) that `noop_patches.py` carried until #1400. `git fetch
--tags` first, or pass `--allow-missing-tags` to state deliberately that you
know the answer is unmeasurable here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# See the module docstring's own measurement for where 8 comes from. It is a
# derived number, not a round one.
DEFAULT_MAX_SHIPPED_COMMITS = 8

_SHIPPED_PATHS = ("custom_components/",)
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


@dataclass
class Drift:
    newest_tag: str | None
    manifest_version: str | None
    shipped_commits: int
    max_shipped_commits: int
    ref: str
    # Each is a finished sentence naming what is wrong, so a caller never has
    # to re-derive the message from the numbers.
    problems: list[str] = field(default_factory=list)

    @property
    def over_threshold(self) -> bool:
        return self.shipped_commits > self.max_shipped_commits

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def evaluate(
    *,
    newest_tag: str | None,
    manifest_version: str | None,
    shipped_commits: int,
    ref: str,
    max_shipped_commits: int = DEFAULT_MAX_SHIPPED_COMMITS,
    allow_missing_tags: bool = False,
) -> Drift:
    """The whole decision, as a pure function of already-gathered facts.

    Split out from the git calls deliberately: every branch below is then
    testable without constructing a repository, which is what lets the
    tests cover the "cannot measure" case at all -- a case that is otherwise
    only reachable by arranging a shallow clone.
    """
    drift = Drift(
        newest_tag=newest_tag,
        manifest_version=manifest_version,
        shipped_commits=shipped_commits,
        max_shipped_commits=max_shipped_commits,
        ref=ref,
    )

    if newest_tag is None:
        if not allow_missing_tags:
            drift.problems.append(
                "no release tag is visible, so release drift could not be "
                "measured at all -- run `git fetch --tags` (a shallow CI "
                "checkout has none by default), or pass --allow-missing-tags "
                "to say deliberately that this is unmeasurable here. A check "
                "that cannot measure has not passed."
            )
        return drift

    if drift.over_threshold:
        drift.problems.append(
            f"{shipped_commits} commits touching {_SHIPPED_PATHS[0]} have "
            f"landed on {ref} since {newest_tag}, over the threshold of "
            f"{max_shipped_commits}. Every one of them is invisible to every "
            "HACS install until a release is tagged. Cut one, or raise "
            "--max-commits deliberately."
        )

    if manifest_version is not None:
        tag_version = newest_tag.lstrip("v")
        if _version_tuple(manifest_version) > _version_tuple(tag_version):
            drift.problems.append(
                f"manifest.json says {manifest_version} but the newest tag is "
                f"{newest_tag} -- a release commit merged and its tag was "
                "never pushed, so the version claims to be released and no "
                "install can see it. Tag it."
            )
        elif _version_tuple(manifest_version) < _version_tuple(tag_version):
            drift.problems.append(
                f"manifest.json says {manifest_version} but the newest tag is "
                f"{newest_tag} -- on a current checkout of {ref} this means a "
                "tag was pushed at a commit whose manifest did not carry that "
                "version, which makes the tag wrong about its own contents. "
                "(Expected and harmless in a checkout that has not pulled.)"
            )

    return drift


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _git(args: list[str], root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out.stdout.strip()


def newest_tag(root: Path = REPO_ROOT) -> str | None:
    """The highest `vX.Y.Z` tag by version order.

    Deliberately NOT `git describe --tags`, which answers "nearest tag
    reachable from HEAD" -- a different question that gives a stale answer on
    a feature branch cut before the latest release.
    """
    listing = _git(["tag", "--list", "v*", "--sort=-v:refname"], root)
    if not listing:
        return None
    for line in listing.splitlines():
        if _TAG_RE.match(line.strip()):
            return line.strip()
    return None


def shipped_commits_since(tag: str, ref: str, root: Path = REPO_ROOT) -> int:
    count = _git(["rev-list", "--count", f"{tag}..{ref}", "--", *_SHIPPED_PATHS], root)
    try:
        return int(count or 0)
    except ValueError:
        return 0


def manifest_version(root: Path = REPO_ROOT) -> str | None:
    path = root / "custom_components" / "nimbus_load" / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def measure(
    *,
    ref: str,
    root: Path = REPO_ROOT,
    max_shipped_commits: int = DEFAULT_MAX_SHIPPED_COMMITS,
    allow_missing_tags: bool = False,
) -> Drift:
    tag = newest_tag(root)
    return evaluate(
        newest_tag=tag,
        manifest_version=manifest_version(root),
        shipped_commits=shipped_commits_since(tag, ref, root) if tag else 0,
        ref=ref,
        max_shipped_commits=max_shipped_commits,
        allow_missing_tags=allow_missing_tags,
    )


def format_report(drift: Drift) -> str:
    """The display form, written for a session opening cold.

    Every line is DERIVED at the moment of printing. That is the entire
    point: `docs/handover/2026-09-21-travel-handover.md` states under a
    heading reading "verified live 13:05 AEST" that production is on
    v0.94.413 and the latest tag is v0.94.415, and by 2026-09-28 the tag was
    v0.94.427 -- so a session that dutifully read the file for its state got
    a confidently wrong answer with an authoritative label on it. A stored
    number rots; a computed one cannot.
    """
    lines = ["Nimbus release state (derived now, not stored):"]
    lines.append(f"  newest release tag       {drift.newest_tag or 'NONE VISIBLE'}")
    lines.append(f"  manifest.json version    {drift.manifest_version or 'unreadable'}")
    lines.append(
        f"  shipped commits since    {drift.shipped_commits} "
        f"(threshold {drift.max_shipped_commits}, on {drift.ref})"
    )
    if drift.problems:
        lines.append("  DRIFT:")
        lines.extend(f"    - {p}" for p in drift.problems)
    else:
        lines.append("  no release drift detected")
    lines.append(
        "  deployed on NUC1 / NUC2 / devhub: UNKNOWN from a checkout -- this "
        "needs a live read, and no file in this repo can answer it."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ref",
        default="origin/main",
        help="the ref to measure drift on (default origin/main; use HEAD in a "
        "checkout with no remote)",
    )
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_SHIPPED_COMMITS)
    parser.add_argument(
        "--allow-missing-tags",
        action="store_true",
        help="state deliberately that no tags are available, so an "
        "unmeasurable result is not a failure",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the state and always exit 0. A DISPLAY, NOT A CHECK -- "
        "see the module docstring before wiring this into anything.",
    )
    args = parser.parse_args(argv)

    ref = args.ref
    if _git(["rev-parse", "--verify", "--quiet", ref], REPO_ROOT) is None:
        # A local clone with no `origin/main` (or a detached CI checkout) is
        # ordinary, not an error worth failing on -- fall back and say so,
        # rather than reporting drift against a ref that does not exist.
        ref = "HEAD"

    drift = measure(
        ref=ref,
        max_shipped_commits=args.max_commits,
        allow_missing_tags=args.allow_missing_tags,
    )
    print(format_report(drift))
    if args.report:
        return 0
    return 1 if drift.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
