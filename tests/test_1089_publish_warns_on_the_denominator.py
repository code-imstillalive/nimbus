"""nimbus #1089: the publish site's wiring, a check that reads source text,
kept apart until the decision on source-reading tests (#1837). The
denominator check itself is in tests/v2/reports/test_quality_score.py."""

from __future__ import annotations

import _solver_path  # noqa: F401
from _writer_source import writer_source


def test_the_publish_site_warns_once_on_an_unusable_denominator():
    text = " ".join(writer_source().split())
    assert 'day_entry.get("epr_denominator_reason") is not None' in text
    assert "_QUALITY_REPORT_EPR_DENOMINATOR_WARNED.add(yesterday_key)" in text
