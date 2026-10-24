"""nimbus #1120: the standalone (cron) quality writer stamps the row it
writes with the release that scored it, as the native writer does. A check
that reads source text, because the cron copy cannot be imported here; kept
apart until the decision on source-reading tests (#1837). The native stamp
is in tests/v2/reports/test_daily_reports.py."""

from __future__ import annotations

from pathlib import Path

_CRON = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_quality_writer.py"
)


def test_the_cron_copy_stamps_the_row_before_it_is_frozen():
    src = _CRON.read_text(encoding="utf-8")
    assert "def _nimbus_version()" in src
    assert '_QUALITY_HISTORY_VERSION_FIELD = "v"' in src
    stamp = src.index("day_entry[_QUALITY_HISTORY_VERSION_FIELD] = _version")
    assert stamp < src.index("quality_history[day_key] = day_entry")
