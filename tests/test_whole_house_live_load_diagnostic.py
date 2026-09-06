"""nimbus issue #429 (Mark Purcell): load_summed_18_now_kw and
load_whole_house_cross_check_now_kw are DELIBERATELY forecast-vs-
forecast (sum of 18 circuit models vs the whole-house meter's own
separate Nimbus forecast model) -- genuinely useful for catching a
missing/misconfigured circuit, but neither is an actual live meter
reading, despite "cross_check" suggesting one. Confirmed live: Mark's
own automated report read load_whole_house_cross_check_now_kw as "the
real whole-house meter cross-check" and flagged a 30.7% gap against it
as a forecast-drift concern -- a real, understandable misreading given
the name, not a report-tool bug.

The genuine live reading (live_load_kw, read directly from the
configured solver_whole_house_cross_check_sensor's own current state)
was already being computed -- used to correct load_kw[0] before the
solve -- but never published anywhere on its own, so no genuine
forecast-vs-reality comparison was actually possible. This test proves
the new load_whole_house_live_now_kw field (added alongside the
existing two, additive only) is populated with the real live reading,
distinct from the forecast-based load_whole_house_cross_check_now_kw.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer

_LOAD_SENSOR = "sensor.nimbus_sigen_plant_total_load_power_forecast"
_CROSS_CHECK_SENSOR = "sensor.whole_house_meter"
_CROSS_CHECK_FORECAST_ENTITY = "sensor.nimbus_whole_house_meter_forecast"

_SOLVER_CONFIG_ATTRS = {
    "solver_battery_soc_sensor": "sensor.fake_soc",
    "solver_battery_capacity_kwh": 40.0,
    "solver_max_charge_kw": 5.0,
    "solver_max_discharge_kw": 5.0,
    "solver_grid_max_import_kw": 15.0,
    "solver_grid_max_export_kw": 15.0,
    "solver_import_price_sensor": "sensor.fake_import_price",
    "solver_export_price_sensor": "sensor.fake_export_price",
    "solver_solar_forecast_sensor": "",
    "solver_load_forecast_sensor": _LOAD_SENSOR,
    "solver_load_forecast_entities": [],
    "solver_whole_house_cross_check_sensor": _CROSS_CHECK_SENSOR,
}

_HEALTHY_LOAD_STATE = {
    "state": "1.5",
    "attributes": {
        "unit_of_measurement": "kW",
        "forecast": [
            {"time": "2026-09-06T20:30:00+10:00", "value": 1.5},
            {"time": "2026-09-06T20:45:00+10:00", "value": 1.4},
            {"time": "2026-09-06T21:00:00+10:00", "value": 1.3},
        ],
    },
}

# Deliberately far from the live reading below (2.28 vs the real 1.744,
# roughly the same real ~31% gap shape Mark's own report found) --
# proves the two published fields are genuinely independent numbers,
# not the same value under two names.
_CROSS_CHECK_FORECAST_STATE = {
    "state": "2.28",
    "attributes": {
        "unit_of_measurement": "kW",
        "forecast": [
            {"time": "2026-09-06T20:30:00+10:00", "value": 2.28},
            {"time": "2026-09-06T20:45:00+10:00", "value": 2.2},
        ],
    },
}

_REAL_LIVE_METER_READING = 1.744


def _make_ha_get():
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: _HEALTHY_LOAD_STATE,
        _CROSS_CHECK_FORECAST_ENTITY: _CROSS_CHECK_FORECAST_STATE,
        _CROSS_CHECK_SENSOR: {
            "state": str(_REAL_LIVE_METER_READING),
            "attributes": {"unit_of_measurement": "kW"},
        },
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestWholeHouseLiveLoadDiagnostic:
    def test_live_field_is_the_real_reading_not_the_forecast(self, freezer):
        ha_get_mock = _make_ha_get()
        posted = {}

        def _capture_ha_post_state(entity_id, state, attrs=None):
            posted[entity_id] = (state, attrs)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(
                solver_writer, "ha_post_state", side_effect=_capture_ha_post_state
            ),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            patch.object(
                solver_writer,
                "PLAN_STATE_PATH",
                "/tmp/nonexistent_plan_state_429_test.json",
            ),
        ):
            solver_writer.main()

        assert solver_writer.ENTITY_ID in posted
        _state, attrs = posted[solver_writer.ENTITY_ID]

        # The new field: the genuine live reading.
        assert attrs["load_whole_house_live_now_kw"] == _REAL_LIVE_METER_READING

        # The existing field stays exactly what it always was: the
        # whole-house meter's own FORECAST model, not the live reading --
        # this fix must not have changed its value or meaning.
        assert attrs["load_whole_house_cross_check_now_kw"] == 2.28

        # The two are genuinely different numbers -- proves this isn't
        # the same value silently duplicated under two field names.
        assert (
            attrs["load_whole_house_live_now_kw"]
            != attrs["load_whole_house_cross_check_now_kw"]
        )

        # Sibling sensor.nimbus_household_load_total_forecast gets the
        # identical new field, same dual-publication convention already
        # used for failed_load_entities/load_forecast_warnings.
        _household_state, household_attrs = posted[
            "sensor.nimbus_household_load_total_forecast"
        ]
        assert household_attrs["whole_house_live_now_kw"] == _REAL_LIVE_METER_READING


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
