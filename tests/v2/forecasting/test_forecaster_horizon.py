"""The Forecaster covers the Solver's whole plan horizon (#1566) (#1837)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import solver_writer
from custom_components.nimbus_load.const import (
    CONF_FORECAST_HORIZON_HOURS,
    DEFAULT_FORECAST_HORIZON_HOURS,
    MIN_FORECAST_HORIZON_HOURS,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.flows.hub_options import _forecaster_schema


def _solver_plan_hours() -> float:
    return solver_writer.TIER0_MINUTES / 60.0 + solver_writer.TOTAL_HORIZON_HOURS


def test_the_minimum_covers_the_solvers_whole_plan():
    """If the Solver's horizon ever grows, this fails instead of the plan
    silently going flat again."""
    assert MIN_FORECAST_HORIZON_HOURS >= _solver_plan_hours()


def test_the_default_is_the_minimum():
    assert DEFAULT_FORECAST_HORIZON_HOURS == MIN_FORECAST_HORIZON_HOURS


def _coord(options):
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.entry = SimpleNamespace(options=options)
    return coord


def test_a_saved_48_hours_is_raised_to_cover_the_plan():
    """The tester's exact saved value."""
    assert (
        _coord({CONF_FORECAST_HORIZON_HOURS: 48.0})._horizon_hours
        == MIN_FORECAST_HORIZON_HOURS
    )


def test_a_longer_saved_horizon_is_kept():
    assert _coord({CONF_FORECAST_HORIZON_HOURS: 168})._horizon_hours == 168


def test_nothing_saved_gives_the_default():
    assert _coord({})._horizon_hours == DEFAULT_FORECAST_HORIZON_HOURS


def test_an_unparseable_saved_value_gives_the_default():
    assert (
        _coord({CONF_FORECAST_HORIZON_HOURS: "x"})._horizon_hours
        == DEFAULT_FORECAST_HORIZON_HOURS
    )


def _horizon_marker(schema):
    for marker in schema.schema:
        if str(getattr(marker, "schema", marker)) == CONF_FORECAST_HORIZON_HOURS:
            return marker, schema.schema[marker]
    raise AssertionError("forecast_horizon_hours not in the Forecaster form")


def test_the_form_keeps_a_longer_saved_value():
    marker, _ = _horizon_marker(_forecaster_schema({CONF_FORECAST_HORIZON_HOURS: 120}))
    assert marker.default() == 120
