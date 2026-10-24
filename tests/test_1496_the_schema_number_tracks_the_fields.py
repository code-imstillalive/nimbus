"""nimbus #1496: the published quality report's literal field set is pinned
to QUALITY_REPORT_SCHEMA, so adding a field without bumping it fails here
instead of freezing older days at their old shape. A check that reads
source, kept apart until the decision on source-reading tests (#1837); the
rescore it drives is in tests/v2/reports/test_daily_reports.py."""

from __future__ import annotations

import ast
import hashlib
import inspect
import sys

import _solver_path  # noqa: F401
import solver_writer

quality = sys.modules[solver_writer.publish_daily_quality_report.__module__]

_EXPECTED_HASHES = {
    2: "0bddf26959893096070fcb8208c89ca885835b611bbaa6c7bee2e9817747149b",
    3: "11f0ea93fda61d94b545172f6035e39af40a9edf38e1cf3dd26e33fe4488a64d",
    4: "9ea656c1af6164288b86f941218fb1309d1fd0bf457f015c1b0c529ea64c522c",
}


def _published_literal_keys() -> list[str]:
    src = inspect.getsource(quality._compute_report_for_window)
    fn = ast.parse(src.lstrip()).body[0]
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return sorted(keys)


def test_the_published_dict_carries_the_stamp():
    keys = _published_literal_keys()
    assert "report_schema" in keys and "j_star_path_delta_explained" in keys


def test_the_field_set_matches_the_schema_number():
    digest = hashlib.sha256("\n".join(_published_literal_keys()).encode()).hexdigest()
    assert digest == _EXPECTED_HASHES.get(quality.QUALITY_REPORT_SCHEMA), (
        "The published quality-report field set changed. Bump "
        "QUALITY_REPORT_SCHEMA in solver_reports/quality.py and add "
        f"{{{quality.QUALITY_REPORT_SCHEMA + 1}: '{digest}'}} here."
    )
