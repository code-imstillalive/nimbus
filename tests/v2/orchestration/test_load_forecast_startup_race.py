"""A load forecast still starting up is not a zero-load plan (#370) (#1837)."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest
import solver_writer
from _isolated_state import isolated_state_paths

_LOAD_SENSOR = "sensor.nimbus_sigen_plant_total_load_power_forecast"

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
}


def _make_ha_get(load_sensor_state: dict):
    """A single ha_get stand-in covering every entity main() touches
    before reaching the load-forecast branch, plus the load-forecast
    entity itself (parameterised so callers can make it look healthy or
    startup-race-unavailable). Anything NOT explicitly listed raises a
    404 HTTPError -- entity_exists() (used to gate every genuinely
    optional integration: Open-Meteo, Solcast, LocalVolts/AEMO extras)
    already treats a 404 as "not configured, skip", so this safely
    no-ops every optional path rather than crashing on it.
    """
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: load_sensor_state,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


class TestStartupRaceLoadForecastNeverPublishesAZeroLoadPlan:
    def test_unavailable_load_sensor_raises_instead_of_publishing(self):
        # The exact live repro: entity restored into the state machine
        # as unavailable, attributes wiped entirely -- no 'forecast'
        # key, no other list-valued attribute either.
        unavailable_state = {"state": "unavailable", "attributes": {}}
        ha_get_mock = _make_ha_get(unavailable_state)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "ha_post_state") as mock_post_state,
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            # nimbus issue #1330: main() writes real /opt state paths by default.
            isolated_state_paths(solver_writer),
        ):
            with pytest.raises(RuntimeError, match="not ready"):
                solver_writer.main()

            # The real regression this test guards against: NOTHING gets
            # published this cycle -- no confidently-wrong zero-load
            # "optimal" plan reaches the state machine at all.
            mock_post_state.assert_not_called()


class TestNeverTrainedLoadForecastNeverPublishesAZeroLoadPlan:
    """nimbus issue #374 -- the review's own suggested regression test."""

    def test_empty_forecast_from_a_never_trained_model_raises_instead_of_publishing(
        self,
    ):
        never_trained_state = {
            "state": "unknown",
            "attributes": {
                "forecast": [],
                "model_trained_at": None,
                "training_points": 0,
            },
        }
        ha_get_mock = _make_ha_get(never_trained_state)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "ha_post_state") as mock_post_state,
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            # nimbus issue #1330: main() writes real /opt state paths by default.
            isolated_state_paths(solver_writer),
        ):
            with pytest.raises(RuntimeError, match="not ready"):
                solver_writer.main()

            # The real #374 regression: nothing gets published this cycle
            # -- no confidently-wrong zero-load "optimal" plan, for as
            # long as this subentry's model stays untrained.
            mock_post_state.assert_not_called()

    def test_empty_forecast_from_an_already_trained_model_does_not_take_the_new_raise_path(
        self,
    ):
        # The counterpart the fix must NOT regress: a subentry with a
        # REAL trained model (real model_trained_at, real training_points)
        # whose forecast is still empty is a genuine, ongoing
        # misconfiguration (e.g. a scheduling window excluding every
        # current period) -- must keep the existing zero-fallback +
        # notification behaviour, not the new #374 raise path.
        trained_but_empty_state = {
            "state": "unknown",
            "attributes": {
                "forecast": [],
                "model_trained_at": "2026-09-01T03:00:00+10:00",
                "training_points": 1200,
            },
        }
        ha_get_mock = _make_ha_get(trained_but_empty_state)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            # nimbus issue #1330: main() writes real /opt state paths by default.
            isolated_state_paths(solver_writer),
        ):
            try:
                solver_writer.main()
            except RuntimeError as e:
                assert "not ready" not in str(e)
            except Exception as e:  # noqa: BLE001
                assert "not ready" not in str(e)
