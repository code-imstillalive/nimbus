"""nimbus issue #594 criterion 1: every release's CHANGELOG entry must
name the devhub validation that was performed.

#594's whole premise is that features were "shipped untested against a
real install → fails on deployment → bug raised from production". The
written directive landed in CLAUDE.md via PR #764. The *doing* has held
up well; the *recording* has not.

Audit history on this issue:

- 2026-09-12 — v0.94.265/.266 were genuinely verified but their entries
  didn't say so. Backfilled by hand (PR #793).
- 2026-09-14 — audited v0.94.284-295 and found **10 of 12** entries
  with no devhub validation named, despite most having genuinely been
  deployed and verified. The same gap, at five times the scale, two days
  later.

A convention that needs a manual backfill every few days is not being
enforced by being written down. This test is the enforcement: a release
entry that doesn't say what was checked fails CI, at the moment the
entry is written, instead of surfacing in an audit weeks later.

**On the in-flight release.** Only versions strictly BELOW the one in
`manifest.json` are checked. A release cannot be validated before it is
cut -- the entry is written at PR time, the deploy happens after the tag
-- so demanding the line in the same commit that bumps the version is an
impossible ordering. (Found the hard way: the first version of this file
did exactly that and failed the very next release it was supposed to
guard.) Exempting only the in-flight version keeps the discipline real:
the moment the *next* release is prepared, the previous one falls inside
the window and must have gained its validation line.

**On the grandfather boundary.** Only releases from _MIN_ENFORCED_VERSION
onward are checked. Earlier entries are left alone deliberately: this
session can only honestly attest to the releases it personally deployed
and verified, and back-writing "verified on devhub" onto someone else's
release would be inventing evidence — which is precisely the failure
mode #594 exists to prevent. The boundary is a statement about what is
knowable, not a loophole; it should never move backwards.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_MANIFEST = _REPO_ROOT / "custom_components" / "nimbus_load" / "manifest.json"

# First release this session personally deployed, verified, and can
# honestly attest to. See the module docstring on why this doesn't
# extend backwards.
_MIN_ENFORCED_VERSION = (0, 94, 289)

_SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*$", re.MULTILINE)

# Deliberately narrow. The point is that the entry states what was
# actually checked on a real install -- not that it contains a hopeful
# adjective. "Devhub validation:" is the phrase the backfill used and
# the one the directive asks for.
_VALIDATION_RE = re.compile(r"devhub validation:", re.IGNORECASE)

# ...but a CHANGELOG entry may also TALK ABOUT validation lines rather
# than carry one, and this guard could not tell the difference. Found
# the honest way, 2026-09-16: v0.94.332's own entry explained why a
# never-tagged version "had no `Devhub validation:` line it could
# honestly carry", and that sentence satisfied the search. The section
# passed while genuinely having no such line -- the guard answering a
# weaker question than it appears to, which is the same shape nimbus
# issue #955 raised about the source-comment guard next door.
#
# Inline code spans are the tell, and a reliable one in both
# directions: a real validation line is written as prose ("Devhub
# validation: deployed and restarted, ..."), while a REFERENCE to the
# concept is written as code, because it is naming the literal string
# the guard looks for. So strip `...` spans before searching.
#
# Fenced blocks are stripped for the same reason -- a ``` block quoting
# a previous entry, or the guard's own regex, should not count as
# performing the validation it quotes.
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def _prose_only(body: str) -> str:
    """The entry with code spans and fenced blocks removed, so that
    naming the phrase cannot be mistaken for stating it."""
    return _CODE_SPAN_RE.sub(" ", _FENCED_BLOCK_RE.sub(" ", body))


def _in_flight_version() -> tuple[int, ...]:
    """The version currently being prepared -- bumped in manifest.json,
    not yet tagged or deployed, so not yet validatable."""
    raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))["version"]
    return tuple(int(p) for p in raw.split("."))


def _sections() -> dict[tuple[int, ...], str]:
    text = _CHANGELOG.read_text(encoding="utf-8")
    out: dict[tuple[int, ...], str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        key = tuple(int(p) for p in m.group(1).split("."))
        out[key] = text[m.end() : end]
    return out


class TestReleasesNameTheirValidation(unittest.TestCase):
    def test_every_enforced_release_states_what_was_validated(self):
        in_flight = _in_flight_version()
        missing = sorted(
            ".".join(str(p) for p in ver)
            for ver, body in _sections().items()
            if _MIN_ENFORCED_VERSION <= ver < in_flight
            and not _VALIDATION_RE.search(_prose_only(body))
        )
        self.assertEqual(
            missing,
            [],
            f"these releases have no 'Devhub validation:' line in their "
            f"CHANGELOG entry: {missing}. nimbus issue #594 criterion 1 "
            f"requires every release to name the validation actually "
            f"performed on a real install. State what was checked and what "
            f"it showed -- including 'devhub cannot validate this because "
            f"X', which is a real and useful answer. Do not write that a "
            f"release was verified unless it was.",
        )

    def test_the_enforced_window_is_not_empty(self):
        """Guards the guard: if _MIN_ENFORCED_VERSION ever drifted above
        the newest release, every assertion above would pass vacuously
        while enforcing nothing."""
        in_flight = _in_flight_version()
        enforced = [v for v in _sections() if _MIN_ENFORCED_VERSION <= v < in_flight]
        self.assertGreater(
            len(enforced),
            0,
            "no releases fall inside the enforced window -- "
            "_MIN_ENFORCED_VERSION is above the newest release, so this "
            "file is enforcing nothing.",
        )

    def test_the_boundary_has_not_moved_backwards(self):
        """The boundary exists because this session could only attest to
        releases it actually verified. Moving it earlier would mean
        someone asserted validation they didn't perform -- the exact
        thing #594 exists to stop."""
        self.assertGreaterEqual(
            _MIN_ENFORCED_VERSION,
            (0, 94, 289),
            "_MIN_ENFORCED_VERSION moved backwards. Older entries were "
            "left unenforced on purpose -- back-writing validation claims "
            "onto releases nobody here verified would be inventing "
            "evidence.",
        )


if __name__ == "__main__":
    unittest.main()
