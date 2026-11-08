"""nimbus #937 on real Home Assistant: why a day has no day-ahead regret score.

`solver_reports.quality._day_ahead_forecast_regret_attributes` imports the
snapshot store relative to the package, so the stub suite only ever reaches
its `native_only` answer (#2393: main's CI covered nothing past it). Here the
package imports resolve. Each refusal is named, never published as a number,
and a scored day carries every figure and its snapshot's capture time. The
reads and the four LP solves are the only things patched. Synthetic only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.nimbus_load import forecast_snapshot_store
from custom_components.nimbus_load.solver import forecast_regret
from custom_components.nimbus_load.solver_inputs import forecast_snapshot
from custom_components.nimbus_load.solver_reports import quality

DAY = datetime(2026, 11, 8, 14, 0, tzinfo=UTC)  # 00:00 Brisbane, 9 Nov
GRID = [DAY + timedelta(hours=h) for h in range(3)]
SNAPSHOT = {"captured_at": "2026-11-08T14:05:00+00:00"}
YESTERDAY = [(t - timedelta(hours=24), 1.0) for t in GRID]
FIGURES = SimpleNamespace(
    j_star=1.0,
    j_forecast=1.5,
    j_persistence=2.0,
    forecast_regret_dollars=0.5,
    persistence_regret_dollars=1.0,
    nimbus_value_add_dollars=0.5,
    load_level_error_dollars=0.2,
    load_shape_error_dollars=None,
    solar_error_dollars=0.1,
)


def _score(
    *,
    snapshot=SNAPSHOT,
    resampled=([0.0] * 3, [1.0] * 3),
    history=YESTERDAY,
    persistence=(1.0, 1.0, 1.0),
    regret=FIGURES,
):
    sw = quality._solver_writer()

    def solve(**_kwargs):
        if isinstance(regret, Exception):
            raise regret
        return regret

    with (
        patch.object(
            forecast_snapshot_store, "get_cached_snapshot", return_value=snapshot
        ),
        patch.object(
            forecast_snapshot, "resample_snapshot_to_grid", return_value=resampled
        ),
        patch.object(sw, "fetch_entity_history_range", return_value=history),
        patch.object(sw, "resample_history_mean", return_value=list(persistence)),
        patch.object(forecast_regret, "compute_forecast_regret", side_effect=solve),
    ):
        return quality._day_ahead_forecast_regret_attributes(
            solar_sensor="sensor.pv_power",
            solar_scale=1.0,
            load_sensor="sensor.house_power",
            load_scale=1.0,
            grid_times=GRID,
            period_hours=1.0,
            periods=None,
            grid=None,
            battery=None,
            solar_real_kw=[0.0] * 3,
            load_real_kw=[1.0] * 3,
            day_start=DAY,
            day_end=DAY + timedelta(days=1),
        )


async def test_a_day_without_a_score_says_why_and_a_scored_day_carries_its_figures(
    hass: HomeAssistant,
):
    for kwargs, reason in (
        ({"snapshot": None}, "no_forecast_snapshot_for_this_day"),
        ({"resampled": None}, "snapshot_unusable_for_this_grid"),
        ({"history": []}, "previous_day_history_unavailable"),
        # An all-zero yesterday is not a baseline: it would flatter the forecast.
        ({"persistence": (0.0, 0.0, 0.0)}, "no_persistence_baseline"),
        ({"regret": RuntimeError("infeasible")}, "decomposition_solve_failed"),
    ):
        out = _score(**kwargs)
        assert out["forecast_regret_reason"] == reason, kwargs
        assert [k for k, v in out.items() if v is not None] == [
            "forecast_regret_reason"
        ]

    out = _score()
    assert out["forecast_regret_reason"] is None
    assert out["forecast_regret_nimbus_value_add_dollars"] == 0.5
    assert out["forecast_regret_load_level_error_dollars"] == 0.2
    # An attribution term the split cannot define stays None, not 0.
    assert out["forecast_regret_load_shape_error_dollars"] is None
    assert out["forecast_regret_snapshot_captured_at"] == SNAPSHOT["captured_at"]
