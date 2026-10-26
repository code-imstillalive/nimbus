"""nimbus #1013: every solve path plans against the derated capacity.

The #538/#1013 class is "wired in one place, missed in another": a fix
that reaches three of four sites is the same bug with a smaller blast
radius. The derating itself, and its publication, are tested in
`tests/v2/orchestration/test_writer_inputs.py` and the golden snapshots
(#1837).
"""

from __future__ import annotations

from pathlib import Path

from _writer_source import writer_source

CRON = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_forecast_writer.py"
).read_text(encoding="utf-8")


def test_no_solve_path_reads_raw_configured_capacity_any_more():
    # test_no_solve_path_reads_raw_configured_capacity_any_more
    for label, src in (("integration", writer_source()), ("cron", CRON)):
        raw = [
            ln
            for ln in src.split("\n")
            if "capacity_kwh = " in ln
            and "solver_battery_capacity_kwh" in ln
            and "nominal = " not in ln
        ]
        assert raw == [], f"{label}: a solve path bypasses the SoH derating: {raw}"

    # test_the_cron_copy_has_the_helper_too: #357: the standalone writer has drifted behind the integration on a
    assert "def resolve_effective_capacity_kwh(" in CRON
