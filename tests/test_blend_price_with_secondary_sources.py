"""Real test of blend_price_with_secondary_sources() (solver_writer.py).

This is the actual orchestration glue wired into main() for 2026-08-25's
blended multi-source price forecasting (nimbus: "u also are missing my
blended price forecasts... in case we can feed it more than one... e.g.
aemo... and amber"). Extracted into its own function specifically so it
can be exercised directly, end to end, without needing to assemble a
full main()-shaped cfg/grid_times/HA-mocking rig.

Uses the REAL blend_forecast_array()/cross_source_spread() (ml/blend.py)
and the REAL resample_generic_price_forecast() -- only ha_get() is
mocked, exactly the established pattern in test_read_load_forecast_
sensor.py and friends.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

_START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _grid_times(n: int) -> list[datetime]:
    return [_START + timedelta(minutes=15 * i) for i in range(n)]


def _forecast_state(values: list[float]) -> dict:
    return {
        "attributes": {
            "forecast": [
                {
                    "time": (_START + timedelta(minutes=15 * i)).isoformat(),
                    "value": v,
                }
                for i, v in enumerate(values)
            ]
        }
    }


class TestNoSecondarySourceConfigured:
    def test_returns_primary_unchanged_and_no_spread(self):
        # The overwhelming-majority-today case: neither _2 nor _3 set.
        # Byte-identical to before this feature existed.
        primary = [0.30, 0.35, 0.40]
        result, spread = solver_writer.blend_price_with_secondary_sources(
            primary,
            {},
            ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
            _grid_times(3),
        )
        assert result == primary
        assert result is primary  # not even a defensive copy -- genuinely untouched
        assert spread is None

    def test_configured_but_entity_unavailable_falls_back_to_primary_unchanged(self):
        primary = [0.30, 0.35, 0.40]
        cfg = {"solver_import_price_sensor_2": "sensor.does_not_exist"}
        with patch.object(solver_writer, "ha_get", side_effect=Exception("404")):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                _grid_times(3),
            )
        assert result == primary
        assert spread is None


class TestOneSecondarySourceConfigured:
    def test_blends_to_the_real_equal_weighted_average(self):
        primary = [0.30, 0.40, 0.50]
        secondary_state = _forecast_state([0.10, 0.20, 0.30])
        cfg = {"solver_import_price_sensor_2": "sensor.aemo_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                _grid_times(3),
            )
        # Real blend_forecast_array() equal-weight average of [0.30,0.10],
        # [0.40,0.20], [0.50,0.30] -- not a mock, the actual function.
        assert [round(v, 6) for v in result] == [0.2, 0.3, 0.4]
        assert spread is not None
        # Real cross_source_spread(): max-min per period == 0.20 everywhere.
        assert [round(v, 6) for v in spread] == [0.20, 0.20, 0.20]

    def test_disagreement_produces_nonzero_spread_proportional_to_gap(self):
        # A source that agrees closely should widen the band far less
        # than one that disagrees sharply -- this is the whole point of
        # cross_source_spread() as an EARNED uncertainty signal, not an
        # arbitrary knob.
        primary = [0.30, 0.30]
        close_state = _forecast_state([0.31, 0.29])
        far_state = _forecast_state([0.80, 0.05])

        cfg_close = {"solver_export_price_sensor_2": "sensor.close_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=close_state):
            _, spread_close = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg_close,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                _grid_times(2),
            )

        cfg_far = {"solver_export_price_sensor_2": "sensor.far_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=far_state):
            _, spread_far = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg_far,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                _grid_times(2),
            )

        assert max(spread_far) > max(spread_close)


class TestNemPd7CalibratedSourceBlendsCorrectly:
    def test_nem_pd7_calibrated_field_is_actually_used_in_the_blend(self):
        # The exact real-world motivating case: blending a retailer's
        # own forecast with Mark Purcell's NEM PD7 sensor, whose
        # forecast entries carry `calibrated`, never `value`.
        primary = [0.40, 0.40]
        nem_pd7_state = {
            "attributes": {
                "forecast": [
                    {
                        "time": _START.isoformat(),
                        "raw_value": 9.999,
                        "calibrated": 0.20,
                    },
                    {
                        "time": (_START + timedelta(minutes=15)).isoformat(),
                        "raw_value": 8.888,
                        "calibrated": 0.20,
                    },
                ]
            }
        }
        cfg = {
            "solver_import_price_sensor_2": (
                "sensor.nem_pd7day_qld1_nem_spot_price_forecast"
            )
        }
        with patch.object(solver_writer, "ha_get", return_value=nem_pd7_state):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                _grid_times(2),
            )
        # (0.40 + 0.20) / 2 == 0.30 -- proves `calibrated` (0.20) was
        # used, not `raw_value` (~9.4, which would have blown the
        # blended price up to ~4.9-5.4, not 0.30).
        assert [round(v, 6) for v in result] == [0.30, 0.30]
        assert spread is not None


class TestBothSecondarySourcesConfigured:
    def test_three_sources_all_blend_together(self):
        primary = [0.60]
        s2 = _forecast_state([0.30])
        s3 = _forecast_state([0.00])
        cfg = {
            "solver_import_price_sensor_2": "sensor.source_2",
            "solver_import_price_sensor_3": "sensor.source_3",
        }

        def _fake_ha_get(entity_id):
            return s2 if entity_id == "sensor.source_2" else s3

        with patch.object(solver_writer, "ha_get", side_effect=_fake_ha_get):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                _grid_times(1),
            )
        # (0.60 + 0.30 + 0.00) / 3 == 0.30, real 3-source equal blend.
        assert [round(v, 6) for v in result] == [0.30]
        assert spread is not None
        assert round(float(spread[0]), 6) == 0.60  # max(0.60,0.30,0.00)-min(...)
