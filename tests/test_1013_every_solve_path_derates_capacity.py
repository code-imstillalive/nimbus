"""nimbus #1013: every solve path plans against the derated capacity.

The #538/#1013 class is "wired in one place, missed in another": a fix
that reaches three of four sites is the same bug with a smaller blast
radius. The derating itself, and its publication, are tested in
`tests/v2/orchestration/test_writer_inputs.py` and the golden snapshots
(#1837).
"""

from __future__ import annotations

from _writer_source import writer_source


def test_no_solve_path_reads_raw_configured_capacity_any_more():
    # test_no_solve_path_reads_raw_configured_capacity_any_more
    # The standalone cron copy was retired (#2289 item 5, B.2a).
    for label, src in (("integration", writer_source()),):
        raw = [
            ln
            for ln in src.split("\n")
            if "capacity_kwh = " in ln
            and "solver_battery_capacity_kwh" in ln
            and "nominal = " not in ln
        ]
        assert raw == [], f"{label}: a solve path bypasses the SoH derating: {raw}"
