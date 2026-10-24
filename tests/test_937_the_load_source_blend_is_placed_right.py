"""nimbus #937: where the load-source blend sits, and how a row keeps its
verdict. Checks that read source text, kept apart until the decision on
source-reading tests (#1837); the behaviour is in
tests/v2/forecasting/test_load_forecast_source.py and
tests/v2/reports/test_daily_reports.py."""

from __future__ import annotations

from pathlib import Path

from _writer_source import writer_source

_LOAD = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "solver_inputs"
    / "load.py"
)


def test_the_blend_runs_after_the_cross_check_snapshot_and_before_the_live_anchor():
    src = _LOAD.read_text(encoding="utf-8")
    blend = src.rindex("_resolve_load_forecast_source(")
    assert (
        src.index("summed_18_now_kw = load_kw[0]")
        < blend
        < src.index("load_kw[0] = max(0.0, live_load_kw)")
    )
    assert (
        "load_lower_kw = blend_load_forecast(" in src
        and "load_upper_kw = blend_load_forecast(" in src
    )


def test_a_row_keeps_its_verdict_and_only_the_value_add_is_kept():
    src = writer_source()
    field = "_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD"
    assert (
        f"elif sw.{field} in prior_row:" in src or f"elif {field} in prior_row:" in src
    )
    for headline_only in (
        "forecast_regret_load_level_error_dollars",
        "forecast_regret_load_shape_error_dollars",
        "forecast_regret_solar_error_dollars",
    ):
        assert f'history[day_key]["{headline_only}"]' not in src
    assert "**_load_forecast_source_attributes(load_forecast_source_decision)" in src
