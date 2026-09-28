"""Golden-master snapshots stay in canonical, reviewable, LF-only text
(nimbus issue #1359).

## What went wrong

The snapshots were `.json.gz`. `_dump()` pinned `mtime=0`, which removes
gzip's timestamp, but nothing can pin zlib's own compressed output: it differs
between zlib builds. So `GOLDEN_UPDATE=1` run on a different interpreter than
the one that recorded a snapshot rewrote **every** file's bytes with not one
recorded value changed -- measured on an issue-1335 branch that touched
nothing under `custom_components/`: all 12 snapshots modified, all 12
decompressing to byte-identical JSON.

Spec 000's update rule is that a behaviour change regenerates the snapshots
and the diff goes in the PR. That rule was worthless while the diff was
thirteen unreadable binary files, and the safe habit -- restore the untouched
ones by hand -- depended on somebody remembering to check.

## Why this file exists rather than a note

Storing them as text makes the invariant *expressible*, which it was not
before: gzip bytes legitimately differ per build, so no byte-level check could
have been written against the old format. Now there is exactly one correct
byte sequence per record, and these tests assert it -- so a snapshot committed
from a stray writer, a hand edit, or a CRLF working tree fails here rather
than surfacing as a mystery diff in someone else's PR.
"""

from __future__ import annotations

import json
import pathlib
import unittest

SNAPSHOTS = pathlib.Path(__file__).parent / "golden" / "snapshots"


def _canonical(record: dict) -> bytes:
    """The one correct byte sequence for a record.

    Deliberately duplicated from `test_golden_master._dump()` rather than
    imported: importing it would make the two agree by construction and this
    file would assert nothing about the format actually written.
    """
    return (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")


class TestGoldenSnapshotFormat(unittest.TestCase):
    def setUp(self):
        self.paths = sorted(SNAPSHOTS.glob("*.json"))

    def test_the_sweep_is_not_vacuous(self):
        """Guards the guard: a glob that matched nothing would make every
        assertion below pass over an empty list."""
        self.assertGreaterEqual(
            len(self.paths),
            14,
            f"expected at least the 14 known snapshots, found "
            f"{[p.name for p in self.paths]} -- if they moved, this test needs "
            f"to follow them rather than quietly passing.",
        )

    def test_every_snapshot_is_in_canonical_form(self):
        """Re-dumping a committed snapshot reproduces its bytes exactly.

        This is the property that makes `GOLDEN_UPDATE=1` safe to run: a
        regeneration that records the same values writes the same bytes, so
        `git status` stays clean and a real change stands alone in the diff.
        """
        for path in self.paths:
            with self.subTest(snapshot=path.name):
                raw = path.read_bytes()
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:  # pragma: no cover
                    self.fail(f"{path.name} is not valid JSON: {exc}")
                self.assertEqual(
                    raw,
                    _canonical(record),
                    f"{path.name} is not in canonical form "
                    f"(json.dumps(sort_keys=True, indent=1) + trailing newline, "
                    f"UTF-8, LF). Regenerate it with GOLDEN_UPDATE=1 rather than "
                    f"editing it by hand -- a hand edit also stops it being a "
                    f"recording of what the solver did.",
                )

    def test_no_snapshot_uses_crlf(self):
        """Stated separately from canonical form because it has its own cause
        and its own fix: git's eol conversion, disabled by the
        `tests/golden/snapshots/** -text` rule in .gitattributes. Without that
        rule this breaks only on a Windows clone, which is the failure shape
        .gitattributes' own comment was written about."""
        for path in self.paths:
            with self.subTest(snapshot=path.name):
                self.assertNotIn(
                    b"\r\n",
                    path.read_bytes(),
                    f"{path.name} contains CRLF. Check that .gitattributes "
                    f"still carries `tests/golden/snapshots/** -text`, then "
                    f"re-checkout the file.",
                )

    def test_no_compressed_snapshot_remains(self):
        """A `.json.gz` here would be read by nothing -- both readers look for
        `.json` since #1359 -- so it would be a silently dead snapshot for a
        scenario that then has no coverage at all."""
        stale = sorted(p.name for p in SNAPSHOTS.glob("*.json.gz"))
        self.assertEqual(
            stale,
            [],
            f"{stale} are compressed snapshots, which nothing reads since "
            f"#1359. Decompress them to .json or delete them -- leaving one "
            f"here means its scenario is silently unsnapshotted.",
        )


if __name__ == "__main__":
    unittest.main()
