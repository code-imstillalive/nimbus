"""Regression test for nimbus repo issue #216 (Mark Purcell): coverage-
aware blending in blend_price_with_secondary_sources().

Real-world scenario reported live: `solver_export_price_sensor_2` was
pointed at a "day 2-7" AEMO-derived forecast with NO real data for
today at all. resample_generic_price_forecast()'s own "hold the most
recent point" step-lookup silently repeated that source's own FIRST
real point for every grid time before its real coverage starts -- and
the old blend_price_with_secondary_sources() averaged that placeholder
50/50 against a fully-real Amber Express primary for the ENTIRE
captured window, producing Mark's reported "~0.5x linear compression
plus a constant offset" (his own measured OLS fit: slope=0.502,
intercept=4.36 c/kWh, R^2=0.992). Confirmed by his own follow-up test:
clearing `_sensor_2` reproduced an exact 1:1 pass-through of the
primary (slope=1.0000, intercept=0.000, R^2=1.0000).

These tests reproduce that shape directly against the real
blend_price_with_secondary_sources()/resample_generic_price_forecast_
with_coverage() functions -- only ha_get() is mocked, same established
pattern as test_blend_price_with_secondary_sources.py.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

_START = datetime(2026, 8, 26, 9, 10, tzinfo=UTC)


def _grid_times(n: int, step_minutes: int = 30) -> list[datetime]:
    return [_START + timedelta(minutes=step_minutes * i) for i in range(n)]


def _forecast_state(points: list[tuple[datetime, float]]) -> dict:
    return {
        "attributes": {
            "forecast": [{"time": t.isoformat(), "value": v} for t, v in points]
        }
    }


class TestSecondarySourceWithNoNearTermCoverage:
    def test_primary_passes_through_unchanged_where_secondary_has_no_real_data_yet(
        self,
    ):
        # Primary (Amber Express-shaped): fully real for the whole
        # captured window.
        grid_times = _grid_times(6)
        primary = [0.72, 1.50, 3.00, 5.00, 8.00, 11.00]

        # Secondary ("day 2-7"): its own real coverage doesn't start
        # until well past this window -- every grid time here resolves
        # to its FIRST point held flat, exactly like Mark's `_sensor_2`.
        secondary_start = _START + timedelta(hours=20)
        secondary_state = _forecast_state(
            [
                (secondary_start, 4.30),
                (secondary_start + timedelta(hours=1), 4.50),
            ]
        )
        cfg = {"solver_export_price_sensor_2": "sensor.nem_pd7day_day_2_7"}

        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                grid_times,
            )

        # Every grid time falls before secondary's real coverage starts
        # -> secondary contributes nothing here -> primary passes
        # through exactly, matching Mark's own clear-`_sensor_2` result.
        assert result == primary
        # cross_source_spread is still computed across the raw resampled
        # arrays (an earned uncertainty signal, unrelated to the point
        # estimate) -- unaffected by this fix, still real and nonzero.
        assert spread is not None

    def test_secondary_contributes_once_its_own_real_coverage_begins(self):
        # Same setup, but now include a grid time that genuinely falls
        # within secondary's own real coverage window -- that period
        # SHOULD still blend (this is a real second opinion there, not
        # a placeholder).
        grid_times = [_START, _START + timedelta(hours=20)]
        primary = [0.72, 9.00]
        secondary_start = _START + timedelta(hours=20)
        secondary_state = _forecast_state([(secondary_start, 4.30)])
        cfg = {"solver_export_price_sensor_2": "sensor.nem_pd7day_day_2_7"}

        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, _spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                grid_times,
            )

        # First period: before secondary's real coverage -> unchanged.
        assert round(result[0], 6) == 0.72
        # Second period: exactly at secondary's one real point -> real
        # equal-weight blend, same as the pre-fix behaviour there.
        assert round(result[1], 6) == round((9.00 + 4.30) / 2, 6)

    def test_primary_real_mask_lets_secondary_win_once_primary_goes_flat(self):
        # Mirrors the has_localvolts branch: once the primary source
        # itself is past ITS real coverage (mask=False), a secondary
        # source that DOES have real data there should get full
        # weight instead of being diluted against primary's own
        # placeholder/extrapolated value.
        grid_times = _grid_times(3)
        primary = [0.72, 1.50, 9.99]  # last point is primary's own placeholder
        primary_real_mask = [True, True, False]
        secondary_state = _forecast_state(
            [(t, 5.0 + i) for i, t in enumerate(grid_times)]
        )
        cfg = {"solver_export_price_sensor_2": "sensor.always_real"}

        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, _spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                grid_times,
                primary_real_mask=primary_real_mask,
            )

        # Periods 0-1: both sources real -> blended as before.
        assert round(result[0], 6) == round((0.72 + 5.0) / 2, 6)
        assert round(result[1], 6) == round((1.50 + 6.0) / 2, 6)
        # Period 2: primary is NOT real (its own placeholder) but
        # secondary IS -> secondary passes through alone, not diluted.
        assert round(result[2], 6) == 7.0
