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

**On the grandfather boundary.** Only releases from _MIN_ENFORCED_VERSION
onward are checked. Earlier entries are left alone deliberately: this
session can only honestly attest to the releases it personally deployed
and verified, and back-writing "verified on devhub" onto someone else's
release would be inventing evidence — which is precisely the failure
mode #594 exists to prevent. The boundary is a statement about what is
knowable, not a loophole; it should never move backwards.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

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
        missing = sorted(
            ".".join(str(p) for p in ver)
            for ver, body in _sections().items()
            if ver >= _MIN_ENFORCED_VERSION and not _VALIDATION_RE.search(body)
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
        enforced = [v for v in _sections() if v >= _MIN_ENFORCED_VERSION]
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
