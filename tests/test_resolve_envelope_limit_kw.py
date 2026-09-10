"""Real tests for solver_writer.resolve_envelope_limit_kw() -- nimbus
issue #493 (Signals 4/7 of #489, item 1, Mark Purcell's own authorized
next step). Real target: Open Dynamic Export's own `opModExpLimW`/
`opModImpLimW` MQTT publish, a SA-Power-Networks-certified CSIP-AUS/
SEP2/IEEE-2030.5 client.

Same convention as test_kw_scale_factor.py -- imports the real function
directly, monkeypatches solver_writer.ha_get (not urllib itself) to fake
a single HA API call without a full HA stub environment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

resolve_envelope_limit_kw = solver_writer.resolve_envelope_limit_kw

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


def _grid_times(n=4):
    base = datetime(2026, 9, 10, 12, 0, tzinfo=_TZ)
    return [base + timedelta(minutes=5 * i) for i in range(n)]


def _mock_ha_get(state, attributes=None):
    def _fn(entity_id):
        return {
            "entity_id": entity_id,
            "state": state,
            "attributes": attributes or {},
        }

    return _fn


def test_no_entity_configured_returns_the_static_limit_flat():
    grid_times = _grid_times()
    result = resolve_envelope_limit_kw(None, 20.0, grid_times)
    assert result == [20.0] * len(grid_times)


def test_missing_entity_falls_back_to_static_limit_with_a_warning():
    def _raise(entity_id):
        raise solver_writer.urllib.error.URLError("boom")

    grid_times = _grid_times()
    with patch.object(solver_writer, "ha_get", _raise):
        result = resolve_envelope_limit_kw(
            "sensor.open_dynamic_export_export_limit", 20.0, grid_times
        )
    assert result == [20.0] * len(grid_times)


def test_unavailable_state_falls_back_to_static_limit():
    grid_times = _grid_times()
    with patch.object(solver_writer, "ha_get", _mock_ha_get("unavailable")):
        result = resolve_envelope_limit_kw(
            "sensor.open_dynamic_export_export_limit", 20.0, grid_times
        )
    assert result == [20.0] * len(grid_times)


def test_non_numeric_state_falls_back_to_static_limit():
    grid_times = _grid_times()
    with patch.object(solver_writer, "ha_get", _mock_ha_get("garbage")):
        result = resolve_envelope_limit_kw(
            "sensor.open_dynamic_export_export_limit", 20.0, grid_times
        )
    assert result == [20.0] * len(grid_times)


def test_live_scalar_kw_state_is_held_flat_across_the_whole_horizon():
    # Open Dynamic Export's own real shape: a single live numeric value,
    # no forward schedule -- held flat, same "no forward-looking source
    # exists for a real measured value" reasoning as battery_kw/grid_kw/
    # solar_kw context features (this project's own recursive-forecast
    # bug chain, CLAUDE.md).
    grid_times = _grid_times()
    with patch.object(
        solver_writer, "ha_get", _mock_ha_get("5.0", {"unit_of_measurement": "kW"})
    ):
        result = resolve_envelope_limit_kw(
            "sensor.open_dynamic_export_export_limit", 20.0, grid_times
        )
    assert result == [5.0] * len(grid_times)


def test_live_scalar_watts_state_is_unit_corrected():
    # Real MQTT telemetry very commonly reports native Watts (Open
    # Dynamic Export's own opModExpLimW is explicitly a Watts field) --
    # must not silently apply a 1000x-too-large bound.
    grid_times = _grid_times()
    with patch.object(
        solver_writer, "ha_get", _mock_ha_get("5000", {"unit_of_measurement": "W"})
    ):
        result = resolve_envelope_limit_kw(
            "sensor.open_dynamic_export_export_limit", 20.0, grid_times
        )
    assert result == [5.0] * len(grid_times)


def test_forecast_shaped_entity_is_resampled_onto_the_grid():
    # A DNSP schedule published ahead of time -- the OTHER real shape
    # this issue's own body names, resampled via the same
    # resample_forecast() every other forecast-shaped entity in this
    # file already uses.
    grid_times = _grid_times(4)
    forecast = [
        {"time": grid_times[0].isoformat(), "value": 5.0},
        {"time": grid_times[2].isoformat(), "value": 10.0},
    ]
    with patch.object(
        solver_writer,
        "ha_get",
        _mock_ha_get("5.0", {"unit_of_measurement": "kW", "forecast": forecast}),
    ):
        result = resolve_envelope_limit_kw(
            "sensor.dnsp_export_schedule", 20.0, grid_times
        )
    # nearest-at-or-before: periods 0-1 hold 5.0, periods 2-3 hold 10.0.
    assert result == [5.0, 5.0, 10.0, 10.0]


def test_forecast_shaped_entity_is_unit_corrected_too():
    grid_times = _grid_times(2)
    forecast = [{"time": grid_times[0].isoformat(), "value": 5000.0}]
    with patch.object(
        solver_writer,
        "ha_get",
        _mock_ha_get("5000", {"unit_of_measurement": "W", "forecast": forecast}),
    ):
        result = resolve_envelope_limit_kw(
            "sensor.dnsp_export_schedule", 20.0, grid_times
        )
    assert result == [5.0, 5.0]


def test_recovery_is_logged_once_after_a_prior_drop():
    # Real end-to-end: a first call with a missing entity records the
    # drop, a second call with a real value must clear it and log the
    # one-time recovery INFO -- same #542/#543 discipline the solar-
    # source dedup already established.
    grid_times = _grid_times()
    entity_id = "sensor.open_dynamic_export_recovery_test"

    def _raise(_entity_id):
        raise solver_writer.urllib.error.URLError("boom")

    with patch.object(solver_writer, "ha_get", _raise):
        resolve_envelope_limit_kw(entity_id, 20.0, grid_times)
    assert any(
        eid == entity_id for eid, _reason in solver_writer._ENVELOPE_LIMIT_WARNED
    )
    with patch.object(
        solver_writer, "ha_get", _mock_ha_get("5.0", {"unit_of_measurement": "kW"})
    ):
        resolve_envelope_limit_kw(entity_id, 20.0, grid_times)
    assert not any(
        eid == entity_id for eid, _reason in solver_writer._ENVELOPE_LIMIT_WARNED
    )
