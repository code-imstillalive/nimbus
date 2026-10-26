"""Tests for `tests/gates/release_drift.py`.

Every decision branch is exercised through `evaluate()`, which is a pure
function of already-gathered facts precisely so these tests need no git
repository -- including the "could not measure" branch, which is otherwise
only reachable by arranging a shallow clone.

**There is deliberately no test asserting that this checkout currently has no
drift.** It would be red in CI for a reason unrelated to drift: `actions/
checkout` fetches no tags by default, so `newest_tag()` returns None and the
gate correctly reports that it cannot measure. Enforcement belongs where the
tags exist -- the SessionStart hook in a full clone, or a CI job with
`fetch-depth: 0`. A test that papered over that by passing
`allow_missing_tags=True` against the real tree would be the exact shape
#1354 and #1400 were both about: a check that cannot fail.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
from gates import release_drift as rd

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTheThreshold(unittest.TestCase):
    def test_over_the_threshold_fails_and_names_both_numbers(self):
        # test_over_the_threshold_fails_and_names_both_numbers
        drift = rd.evaluate(
            newest_tag="v0.94.426",
            manifest_version="0.94.426",
            shipped_commits=16,
            ref="main",
            max_shipped_commits=8,
        )
        self.assertTrue(drift.failed)
        self.assertIn("16 commits", drift.problems[0])
        self.assertIn("threshold of 8", drift.problems[0])

        # test_exactly_at_the_threshold_is_not_drift: A boundary worth pinning rather than leaving to `>` vs `>=`: the
        at = rd.evaluate(
            newest_tag="v1.0.0",
            manifest_version="1.0.0",
            shipped_commits=8,
            ref="main",
            max_shipped_commits=8,
        )
        over = rd.evaluate(
            newest_tag="v1.0.0",
            manifest_version="1.0.0",
            shipped_commits=9,
            ref="main",
            max_shipped_commits=8,
        )
        self.assertFalse(at.failed)
        self.assertTrue(over.failed)

        # test_a_clean_state_reports_nothing
        drift = rd.evaluate(
            newest_tag="v0.94.427",
            manifest_version="0.94.427",
            shipped_commits=1,
            ref="origin/main",
        )
        self.assertFalse(drift.failed)
        self.assertEqual(drift.problems, [])


class TestTheRealIncidentStaysCaught(unittest.TestCase):
    """The v0.94.426 -> v0.94.427 gap, pinned as a regression test.

    Measured from real history: 16 commits touching `custom_components/`
    landed on `main` between the v0.94.426 tag and the moment before the
    v0.94.427 release commit was cut. Three issues (#1360, #768, #1357) were
    each blocked on that one untaken action, and #1361's diagnostics and
    #1363's reconstruction both merged after production had already deployed.

    This test exists so the default threshold cannot later be raised past the
    incident it was derived from without a test going red and forcing the
    reasoning to be written down.
    """

    def test_the_default_threshold_would_have_caught_it(self):
        # test_the_default_threshold_would_have_caught_it
        drift = rd.evaluate(
            newest_tag="v0.94.426",
            manifest_version="0.94.426",
            shipped_commits=16,
            ref="main",
        )
        self.assertTrue(
            drift.failed,
            "the default threshold must still catch the 16-commit gap it was "
            f"derived from (currently {rd.DEFAULT_MAX_SHIPPED_COMMITS})",
        )

        # test_the_ordinary_cadence_does_not_trip_it: The other half, and the reason the threshold is 8 rather than 2:
        for count in (1, 1, 2, 2, 3, 3, 3, 4, 6, 6):
            with self.subTest(shipped_commits=count):
                drift = rd.evaluate(
                    newest_tag="v0.94.420",
                    manifest_version="0.94.420",
                    shipped_commits=count,
                    ref="main",
                )
                self.assertFalse(drift.failed, f"{count} must not trip the gate")


class TestCannotMeasureIsAFailure(unittest.TestCase):
    """Applying #1400's own lesson to a new gate on the day it is written."""

    def test_no_visible_tag_fails_rather_than_passing(self):
        # test_no_visible_tag_fails_rather_than_passing
        drift = rd.evaluate(
            newest_tag=None,
            manifest_version="0.94.427",
            shipped_commits=0,
            ref="main",
        )
        self.assertTrue(drift.failed)
        self.assertIn("could not be measured", drift.problems[0])
        self.assertIn("has not passed", drift.problems[0])

        # test_missing_tags_can_be_declared_deliberately
        drift = rd.evaluate(
            newest_tag=None,
            manifest_version="0.94.427",
            shipped_commits=0,
            ref="main",
            allow_missing_tags=True,
        )
        self.assertFalse(drift.failed)


class TestManifestVersusTag(unittest.TestCase):
    def test_manifest_ahead_means_a_tag_was_never_pushed(self):
        # test_manifest_ahead_means_a_tag_was_never_pushed
        drift = rd.evaluate(
            newest_tag="v0.94.426",
            manifest_version="0.94.427",
            shipped_commits=0,
            ref="main",
        )
        self.assertTrue(drift.failed)
        self.assertIn("never", drift.problems[0])
        self.assertIn("Tag it", drift.problems[0])

        # test_manifest_behind_means_the_tag_misstates_its_contents
        drift = rd.evaluate(
            newest_tag="v0.94.427",
            manifest_version="0.94.426",
            shipped_commits=0,
            ref="main",
        )
        self.assertTrue(drift.failed)
        self.assertIn("wrong about its own contents", drift.problems[0])

        # test_an_unreadable_manifest_is_not_invented_as_a_mismatch
        drift = rd.evaluate(
            newest_tag="v0.94.427",
            manifest_version=None,
            shipped_commits=0,
            ref="main",
        )
        self.assertFalse(drift.failed)


class TestTheReportIsDerivedNotStored(unittest.TestCase):
    def test_the_report_always_says_the_deployed_version_is_unknown(self):
        # test_the_report_always_says_the_deployed_version_is_unknown: The whole reason this tool exists rather than another handover
        text = rd.format_report(
            rd.evaluate(
                newest_tag="v0.94.427",
                manifest_version="0.94.427",
                shipped_commits=0,
                ref="origin/main",
            )
        )
        self.assertIn("UNKNOWN", text)
        self.assertIn("needs a live read", text)

        # test_problems_appear_in_the_report_body
        text = rd.format_report(
            rd.evaluate(
                newest_tag="v0.94.426",
                manifest_version="0.94.426",
                shipped_commits=99,
                ref="main",
            )
        )
        self.assertIn("DRIFT:", text)
        self.assertIn("99 commits", text)


class TestCli(unittest.TestCase):
    """Smoke-level only, and deliberately so -- see this module's docstring
    for why there is no assertion about the real repo's drift state.
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tests" / "gates" / "release_drift.py"),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_report_mode_always_exits_zero_and_prints_a_report(self):
        # test_report_mode_always_exits_zero_and_prints_a_report
        result = self._run("--report")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Nimbus release state", result.stdout)
        self.assertIn("UNKNOWN", result.stdout)

        # test_report_mode_runs_whether_or_not_tags_are_available: It must not crash in a shallow checkout with no tags -- that is
        result = self._run("--report", "--ref", "HEAD")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
