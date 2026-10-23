"""nimbus #1120: a scored day is frozen forever, so the table has to say
which release froze it.

`_carry_forward_quality_history()` writes a day's five headline numbers
into the quality report's `history` table once, the morning after, and
nothing ever recomputes an entry. Every scoring-formula change therefore
splits that table into two incomparable halves that render side by side
on the same card with nothing marking the seam.

**Measured on the reference household, 2026-09-18, hours after v0.94.391
landed.** Two cards, same sensor, same moment:

    Yesterday    2026-09-17   EPR  94.8%   regret  $1.67
    2 days ago   2026-09-16   EPR 106.4%   regret -$0.79

The second row carries `j_ach` -$7.62 beating `j_star` -$6.84 -- the
`oracle_beaten` signature #1081 fixed by pricing EPR and regret through
`j_star_evaluator`. The fix was live on that install. The row simply
predated it and nothing rescores.

This change cannot make that row right; only a persisting rescore path
could, and that is #1120's own second half. It makes the row **visible**,
which is the difference between a household reading a wrong number and
reading a qualified one.

Two properties are load-bearing and both are pinned here:

1. **Only the row being written now is stamped.** Back-dating prior rows
   to the current version would assert something false about who scored
   them, and prior rows may have come from the standalone writer or from
   before this change existed. Unstamped means "scored by something that
   did not say", which is exactly right for them.
2. **A missing or malformed manifest costs nothing.** `_nimbus_version()`
   returns None rather than raising, and an unstamped row then reads
   precisely as every pre-#1120 row does. A day's score must never be
   lost to a metadata read.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import _solver_path  # noqa: F401
import solver_writer


def _entry(epr=90.0, j_ref=5.0, j_ach=-20.0, j_star=-21.0, regret=1.0, **extra):
    return {
        "epr": epr,
        "j_ref": j_ref,
        "j_ach": j_ach,
        "j_star": j_star,
        "regret_dollars": regret,
        **extra,
    }


class TestTheRowBeingWrittenIsStamped(unittest.TestCase):
    def setUp(self):
        solver_writer._nimbus_version.cache_clear()

    def tearDown(self):
        solver_writer._nimbus_version.cache_clear()

    def test_a_fresh_row_carries_the_running_version(self):
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.392"
        ):
            history = solver_writer._carry_forward_quality_history(
                {}, "2026-09-18", _entry()
            )
        self.assertEqual(history["2026-09-18"]["v"], "0.94.392")

    def test_the_five_headline_numbers_are_unchanged(self):
        """The stamp is additive. `nimbus-regret-card.js` reads these five
        by name, and a change to any of them is a card-breaking change."""
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.392"
        ):
            history = solver_writer._carry_forward_quality_history(
                {}, "2026-09-18", _entry()
            )
        row = history["2026-09-18"]
        for field in ("epr", "j_ref", "j_ach", "j_star", "regret_dollars"):
            with self.subTest(field=field):
                self.assertIn(field, row)
        self.assertEqual(row["epr"], 90.0)
        self.assertEqual(row["regret_dollars"], 1.0)

    def test_the_key_is_one_character(self):
        """Deliberate, not terseness for its own sake: this dict rides in
        the payload #944 measures against the recorder's 16 KB cap, and
        that payload measured 20,738 bytes on a real install. At the
        60-day cap, `v` costs ~960 bytes where a descriptive name costs
        ~2.4 KB."""
        self.assertEqual(solver_writer._QUALITY_HISTORY_VERSION_FIELD, "v")


class TestPriorRowsAreNeverBackDated(unittest.TestCase):
    """The property that makes the stamp trustworthy. A row stamped with a
    version that did not score it is worse than no stamp at all, because
    it reads as evidence."""

    def setUp(self):
        solver_writer._nimbus_version.cache_clear()

    def test_an_existing_unstamped_row_stays_unstamped(self):
        prior = {"history": {"2026-09-16": _entry(epr=106.4, regret=-0.79)}}
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.392"
        ):
            history = solver_writer._carry_forward_quality_history(
                prior, "2026-09-18", _entry()
            )
        self.assertNotIn(
            "v",
            history["2026-09-16"],
            "a row written before this change was back-dated to the "
            "running version. It was not scored by that version, and "
            "saying so turns a silent problem into a confident wrong "
            "answer -- the exact failure #1120 exists to prevent",
        )
        self.assertEqual(history["2026-09-18"]["v"], "0.94.392")

    def test_an_existing_row_keeps_its_own_older_stamp(self):
        prior = {"history": {"2026-09-16": _entry(v="0.94.375")}}
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.392"
        ):
            history = solver_writer._carry_forward_quality_history(
                prior, "2026-09-18", _entry()
            )
        self.assertEqual(history["2026-09-16"]["v"], "0.94.375")
        self.assertEqual(history["2026-09-18"]["v"], "0.94.392")

    def test_the_mixed_table_is_readable_as_mixed(self):
        """The whole point, stated as the question a reader asks: are
        these rows comparable?"""
        prior = {
            "history": {
                "2026-09-15": _entry(),
                "2026-09-16": _entry(v="0.94.375"),
            }
        }
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.392"
        ):
            history = solver_writer._carry_forward_quality_history(
                prior, "2026-09-17", _entry()
            )
        versions = {k: v.get("v") for k, v in history.items()}
        self.assertEqual(
            versions,
            {"2026-09-15": None, "2026-09-16": "0.94.375", "2026-09-17": "0.94.392"},
        )


class TestAMissingManifestCostsNothing(unittest.TestCase):
    def setUp(self):
        solver_writer._nimbus_version.cache_clear()

    def tearDown(self):
        solver_writer._nimbus_version.cache_clear()

    def test_no_stamp_rather_than_a_crash(self):
        with mock.patch.object(solver_writer, "_nimbus_version", return_value=None):
            history = solver_writer._carry_forward_quality_history(
                {}, "2026-09-18", _entry()
            )
        self.assertNotIn("v", history["2026-09-18"])
        self.assertEqual(history["2026-09-18"]["epr"], 90.0)

    def test_an_unreadable_manifest_returns_none(self):
        with mock.patch("builtins.open", side_effect=OSError("boom")):
            self.assertIsNone(solver_writer._nimbus_version())

    def test_malformed_json_returns_none(self):
        m = mock.mock_open(read_data="{not json")
        with mock.patch("builtins.open", m):
            self.assertIsNone(solver_writer._nimbus_version())

    def test_a_manifest_without_a_version_returns_none(self):
        m = mock.mock_open(read_data=json.dumps({"domain": "nimbus_load"}))
        with mock.patch("builtins.open", m):
            self.assertIsNone(solver_writer._nimbus_version())

    def test_a_non_string_version_returns_none(self):
        m = mock.mock_open(read_data=json.dumps({"version": 94}))
        with mock.patch("builtins.open", m):
            self.assertIsNone(solver_writer._nimbus_version())


class TestItReadsThisPackagesRealManifest(unittest.TestCase):
    """Not mocked: the helper must find the real file in a real checkout,
    or the whole thing is a no-op that every mocked test still passes."""

    def setUp(self):
        solver_writer._nimbus_version.cache_clear()

    def tearDown(self):
        solver_writer._nimbus_version.cache_clear()

    def test_it_matches_manifest_json_on_disk(self):
        import os

        manifest = os.path.join(
            os.path.dirname(os.path.abspath(solver_writer.__file__)), "manifest.json"
        )
        with open(manifest, encoding="utf-8") as handle:
            expected = json.load(handle)["version"]
        self.assertEqual(solver_writer._nimbus_version(), expected)


class TestTheCronCopyStampsToo(unittest.TestCase):
    """#357's drift class: the standalone/cron writer keeps its own rolling
    table in its own JSON file and posts it as the same `history`
    attribute. A stamp that exists only natively would leave the
    deployment shape the reference household actually runs unmarked --
    which is the install the measurement in this file's docstring came
    from.

    Source-checked because the cron copy cannot be imported here: it
    `sys.path.insert`s an absolute deployment path and imports `solver`
    from it at module scope.
    """

    def _source(self) -> str:
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(solver_writer.__file__))),
            "..",
            "docs",
            "real-world-integration",
            "files",
            "nimbus_solver_quality_writer.py",
        )
        with open(os.path.normpath(path), encoding="utf-8") as handle:
            return handle.read()

    def test_it_defines_the_same_helper(self):
        self.assertIn("def _nimbus_version()", self._source())

    def test_it_uses_the_same_field_name(self):
        source = self._source()
        self.assertIn('_QUALITY_HISTORY_VERSION_FIELD = "v"', source)
        self.assertIn(
            "day_entry[_QUALITY_HISTORY_VERSION_FIELD] = _version",
            source,
            "the cron copy computes a version but never writes it onto "
            "the row -- the stamp would be a no-op on the deployment "
            "shape this issue was measured on",
        )

    def test_it_stamps_before_freezing_the_row(self):
        """Order matters: `quality_history[day_key] = day_entry` is the
        freeze. Stamping after it would mutate a dict already stored,
        which happens to work today only because it is the same object."""
        source = self._source()
        stamp = source.index("day_entry[_QUALITY_HISTORY_VERSION_FIELD] = _version")
        freeze = source.index("quality_history[day_key] = day_entry")
        self.assertLess(stamp, freeze)


if __name__ == "__main__":
    unittest.main()
