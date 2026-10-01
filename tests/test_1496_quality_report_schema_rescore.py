"""nimbus issue #1496: a day scored by an older release must pick up the fields
a newer release adds.

Measured on a real install (Mark Purcell): 30 Sep was first scored before
v0.94.433 reached it. After the upgrade the published sensor still read
`j_star_path_delta_explained: None`, while `compute_quality_report` for the
identical window returned 1.2357 -- because the "already scored" fast path
re-pushes the published attributes verbatim, forever.

Pinned here:

1. The published report carries `report_schema = QUALITY_REPORT_SCHEMA`.
2. The fast path re-scores a day whose published schema is older -- **once per
   process**, so a recompute that keeps failing (and is held by #1463) cannot
   become an LP solve on every ~17 s cycle.
3. A current-schema day takes the plain fast path (no recompute).
4. **The guard that makes the number honest:** the literal key set of the
   published dict is pinned to the schema number, so adding a field without
   bumping `QUALITY_REPORT_SCHEMA` fails here instead of silently freezing old
   days at their old shape again.
"""

import ast
import hashlib
import inspect
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

quality = sys.modules[solver_writer.publish_daily_quality_report.__module__]

BRISBANE = solver_writer.LOCAL_TZ
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=BRISBANE)
YESTERDAY = "2026-09-30"


def _published_literal_keys() -> list[str]:
    src = inspect.getsource(quality._compute_report_for_window)
    tree = ast.parse(src.lstrip()) if src.startswith(" ") else ast.parse(src)
    fn = tree.body[0]
    returns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
    ]
    keys = set()
    for r in returns:
        for k in r.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return sorted(keys)


def _key_hash() -> str:
    return hashlib.sha256("\n".join(_published_literal_keys()).encode()).hexdigest()


class TestTheSchemaIsPinnedToTheFieldSet(unittest.TestCase):
    def test_the_published_dict_carries_the_stamp(self):
        keys = _published_literal_keys()
        self.assertIn("report_schema", keys)
        self.assertIn("j_star_path_delta_explained", keys)

    def test_the_field_set_matches_the_schema_number(self):
        expected = _EXPECTED_HASHES.get(quality.QUALITY_REPORT_SCHEMA)
        self.assertEqual(
            _key_hash(),
            expected,
            "The published quality-report field set changed. Bump "
            "QUALITY_REPORT_SCHEMA in solver_reports/quality.py (so days scored "
            "by an older release are re-scored once, #1496) and add "
            f"{{{quality.QUALITY_REPORT_SCHEMA + 1}: '{_key_hash()}'}} to "
            "_EXPECTED_HASHES here.",
        )


#: schema -> hash of the literal key set. See the class above.
_EXPECTED_HASHES = {
    2: "0bddf26959893096070fcb8208c89ca885835b611bbaa6c7bee2e9817747149b",
}


class TestTheFastPathRescoresAnOlderSchemaOnce(unittest.TestCase):
    def setUp(self):
        quality._SCHEMA_RESCORE_TRIED.clear()
        self.addCleanup(quality._SCHEMA_RESCORE_TRIED.clear)

    def _run(self, attrs):
        existing = {"state": 76.78, "attributes": dict(attrs)}
        with (
            patch.object(solver_writer, "ha_get", return_value=existing),
            patch.object(
                solver_writer, "compute_daily_quality_report", return_value=None
            ) as compute,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_daily_quality_report({}, NOW)
        return compute, post

    def _attrs(self, **over):
        a = {
            "latest_date": YESTERDAY,
            "real_p2p_settlement_status": "no_sensor_configured",
            "generated_at": NOW.isoformat(),
        }
        a.update(over)
        return a

    def test_a_pre_stamp_day_is_rescored(self):
        """The real case: published before `report_schema` existed."""
        compute, _ = self._run(self._attrs())
        compute.assert_called_once()

    def test_a_failed_rescore_is_not_retried_within_the_backoff(self):
        """compute returns None here (a failed re-score, held by #1463):
        the very next cycle must not re-solve."""
        with patch.object(quality.time, "monotonic", return_value=1000.0):
            self._run(self._attrs())
        with patch.object(quality.time, "monotonic", return_value=1000.0 + 17):
            compute, post = self._run(self._attrs())
        compute.assert_not_called()
        post.assert_called_once()

    def test_a_failed_rescore_is_retried_after_the_backoff(self):
        """The real failure: the first try ran during startup, the recorder
        returned no rows, and 'once per process' never tried again."""
        with patch.object(quality.time, "monotonic", return_value=1000.0):
            self._run(self._attrs())
        later = 1000.0 + quality._SCHEMA_RESCORE_RETRY_SECONDS + 1
        with patch.object(quality.time, "monotonic", return_value=later):
            compute, _ = self._run(self._attrs())
        compute.assert_called_once()

    def test_a_current_schema_day_takes_the_plain_fast_path(self):
        compute, post = self._run(
            self._attrs(report_schema=quality.QUALITY_REPORT_SCHEMA)
        )
        compute.assert_not_called()
        post.assert_called_once()

    def test_an_unreadable_stamp_counts_as_old(self):
        compute, _ = self._run(self._attrs(report_schema="garbage"))
        compute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
