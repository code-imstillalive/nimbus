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
    def test_primary_wins_unblended_when_real_even_with_secondary_configured(self):
        # Primary-preferring (nimbus issue #239): no primary_real_mask
        # passed here defaults to "always real" -- so the primary must
        # win outright, never averaged with the secondary, regardless of
        # how much the two disagree.
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
        assert result == primary
        # cross_source_spread is still computed across the raw resampled
        # arrays regardless of which one wins the point estimate -- an
        # earned uncertainty signal, unaffected by #239.
        assert spread is not None
        assert [round(v, 6) for v in spread] == [0.20, 0.20, 0.20]

    def test_falls_back_to_secondary_alone_when_primary_lacks_real_coverage(self):
        # The secondary's own real job (nimbus issue #239): filling a
        # gap where the PRIMARY has no real coverage. Real equal-weight
        # blend_forecast_array() collapses to the secondary alone here
        # since it's the only real source once primary is marked False.
        primary = [0.30, 0.40, 0.50]
        secondary_state = _forecast_state([0.10, 0.20, 0.30])
        cfg = {"solver_import_price_sensor_2": "sensor.aemo_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                _grid_times(3),
                primary_real_mask=[False, False, False],
            )
        assert [round(v, 6) for v in result] == [0.10, 0.20, 0.30]
        assert spread is not None
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
                primary_real_mask=[False, False],
            )
        # Primary marked NOT real (nimbus issue #239: primary-preferring
        # means the secondary only ever surfaces this way) -- proves
        # `calibrated` (0.20) was used, not `raw_value` (~9.4, which
        # would have blown the passthrough value up to ~8.9-9.4, not
        # 0.20).
        assert [round(v, 6) for v in result] == [0.20, 0.20]
        assert spread is not None


class TestBothSecondarySourcesConfigured:
    def test_primary_wins_alone_even_with_two_secondaries_configured(self):
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
        # Primary-preferring (nimbus issue #239): the number of real
        # secondaries configured doesn't matter -- a real primary always
        # wins alone.
        assert [round(v, 6) for v in result] == [0.60]
        assert spread is not None
        assert round(float(spread[0]), 6) == 0.60  # max(0.60,0.30,0.00)-min(...)

    def test_two_secondaries_blend_together_when_primary_lacks_real_coverage(self):
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
                primary_real_mask=[False],
            )
        # (0.30 + 0.00) / 2 == 0.15 -- primary excluded from the mean
        # once it's marked not real, only the two real secondaries blend.
        assert [round(v, 6) for v in result] == [0.15]
        assert spread is not None
        assert round(float(spread[0]), 6) == 0.60  # max(0.60,0.30,0.00)-min(...)


class TestPrimaryPreferringRegression:
    """Direct reproduction of nimbus issue #236 (Mark Purcell): a real
    Amber Express export price (-$0.0037) was averaged 50/50 against a
    real QLD1 PD7DAY wholesale forecast (+$0.6701), inverting the
    import/export price relationship and causing the LP to plan
    simultaneous 22kW grid import + 30kW grid export. Fixed in #239 by
    making the primary win outright whenever it's real, never averaged
    with a secondary just because one happens to be live too.
    """

    def test_disagreeing_real_sources_do_not_average_primary_wins(self):
        primary = [-0.0037]
        secondary_state = _forecast_state([0.6701])
        cfg = {"solver_export_price_sensor_2": "sensor.qld1_pd7day_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
                _grid_times(1),
            )
        # Before #239 this would have been (-0.0037 + 0.6701) / 2 ==
        # 0.3332 -- exactly Mark's reported inflated export_price.
        assert result == primary
        assert round(result[0], 4) == -0.0037
        # cross_source_spread still reports the real disagreement --
        # #239 changes which value is USED, not whether the two sources'
        # disagreement is still visible as an earned uncertainty signal.
        assert spread is not None
        assert round(float(spread[0]), 4) == round(0.6701 - (-0.0037), 4)

    def test_nothing_real_anywhere_still_falls_back_to_equal_weight_mean(self):
        # The honest "everyone's guessing" case (unchanged by #239):
        # when NEITHER the primary nor any secondary has real coverage
        # at a period, fall back to the old equal-weight mean across
        # every source's own held-flat value, same as before #216/#239.
        # Secondary's real coverage starts well after this grid time, so
        # resample_generic_price_forecast_with_coverage() marks it False
        # here too -- the same "day 2-7 forecast queried before day 2"
        # shape as the #216 coverage-aware tests.
        grid_time = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        secondary_start = grid_time + timedelta(hours=20)
        primary = [0.50]
        secondary_state = _forecast_state([0.10])
        secondary_state["attributes"]["forecast"][0]["time"] = (
            secondary_start.isoformat()
        )
        cfg = {"solver_import_price_sensor_2": "sensor.far_future_forecast"}
        with patch.object(solver_writer, "ha_get", return_value=secondary_state):
            result, _spread = solver_writer.blend_price_with_secondary_sources(
                primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                [grid_time],
                primary_real_mask=[False],
            )
        # (0.50 + 0.10) / 2 == 0.30 -- neither source is real here, so
        # every configured source's own held-flat value still
        # contributes, exactly like before #239.
        assert round(result[0], 6) == 0.30
