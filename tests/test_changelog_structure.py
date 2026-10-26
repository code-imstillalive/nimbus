"""Structural guards on CHANGELOG.md, so cutting a release can't
silently rewrite the wrong section.

Written after a real near-miss on 2026-09-14: two agents working this
repo concurrently both cut v0.94.293 (PRs #865 and #866, both merged).
That collision happened to resolve cleanly, but it exposed a live
foot-gun sitting in the file -- a SECOND `## [Unreleased]` heading
buried at line ~1363, between the v0.94.85 and v0.94.84 entries.

Why that matters: the standard way to cut a release here is to rename
`## [Unreleased]` to `## [X.Y.Z] - <date>`. Any implementation that
does a plain `replace(...)` without a count, a `rindex`, or an
unanchored regex would have rewritten the wrong section -- burying a
released version heading in the middle of the file and silently
relabelling three-week-old shipped work as the current release. The
scripts used on the day happened to anchor on a compound string and
were safe; the next one might not be.

The stray section's own content (`.gitignore` entries, executable bits,
`icon.psd` relocation -- nimbus issue #364 findings 5/6/7) was not
genuinely unreleased either: this same file's "#364 findings 1 and 2"
entry states plainly that v0.94.85's earlier pass shipped exactly those
three, and the block already sat directly beneath the v0.94.85 heading.
So it was folded into v0.94.85, where the file itself says it belongs.

Every assertion below was checked against the real file before being
written, rather than asserted hopefully: of 332 headings, the duplicated
`Unreleased` was the only violation. The 330 numeric version headings
were already unique and already strictly descending.
"""

from __future__ import annotations

import collections
import itertools
import json
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_MANIFEST = _REPO_ROOT / "custom_components" / "nimbus_load" / "manifest.json"

_HEADING_RE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _headings() -> list[str]:
    return _HEADING_RE.findall(_CHANGELOG.read_text(encoding="utf-8"))


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in v.split("."))


class TestChangelogStructure(unittest.TestCase):
    def test_exactly_one_unreleased_heading(self):
        # test_exactly_one_unreleased_heading: The actual guard. More than one makes "rename the Unreleased
        heads = _headings()
        self.assertEqual(
            heads.count("Unreleased"),
            1,
            "CHANGELOG.md has more than one '## [Unreleased]' heading. "
            "Release tooling renames that heading to cut a version, so a "
            "duplicate means a release can rewrite the wrong section. Fold "
            "the stray block into the version that actually shipped it.",
        )

        # test_unreleased_is_the_first_heading: Belt and braces: even a correct single-match rename is only
        heads = _headings()
        self.assertEqual(
            heads[0],
            "Unreleased",
            f"the first '## [...]' heading is {heads[0]!r}, not 'Unreleased' "
            "-- new entries have nowhere to accumulate, and a release script "
            "that rewrites the first heading would clobber a shipped version.",
        )

        # test_no_duplicate_version_headings: Two sections for one version means one of them is lying about
        versions = [h for h in _headings() if _SEMVER_RE.match(h)]
        dupes = sorted(v for v, c in collections.Counter(versions).items() if c > 1)
        self.assertEqual(dupes, [], f"duplicate version headings: {dupes}")

        # test_version_headings_are_strictly_descending: Newest first, no repeats, nothing out of order -- which also
        versions = [h for h in _headings() if _SEMVER_RE.match(h)]
        self.assertGreater(len(versions), 100, "sanity: the walk found real content")
        out_of_order = [
            (a, b)
            for a, b in itertools.pairwise(versions)
            if _version_key(a) <= _version_key(b)
        ]
        self.assertEqual(
            out_of_order,
            [],
            f"version headings are not strictly descending: {out_of_order[:5]}",
        )

        # test_the_shipped_manifest_version_has_a_changelog_section: Catches both halves of the release-step mistake this repo has
        version = json.loads(_MANIFEST.read_text(encoding="utf-8"))["version"]
        self.assertIn(
            f"## [{version}]",
            _CHANGELOG.read_text(encoding="utf-8"),
            f"manifest.json is on {version} but CHANGELOG.md has no "
            f"'## [{version}]' section -- either the release entry was never "
            "written, or the version was bumped without one.",
        )


if __name__ == "__main__":
    unittest.main()
