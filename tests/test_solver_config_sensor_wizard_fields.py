"""NimbusSolverConfigSensor must publish the two new issue #56 wizard fields.

Raf's commit a72260d ("Fix issue #56/#60") made solver_writer.py read
cfg["solver_load_forecast_entities"] and cfg["solver_whole_house_cross_check_sensor"]
instead of the old hardcoded module constants. cfg comes from
sensor.nimbus_solver_config's attributes (fetch_solver_config() reads them
over REST -- see its own docstring for why not entry.options).

Without adding both new CONF keys to _SOLVER_ALL_KEYS in sensor.py,
NimbusSolverConfigSensor never publishes them, so a user who fills in
either wizard field via Configure -> Solver settings sees no effect at
solve time -- fetch_solver_config() returns a cfg dict missing those keys,
cfg.get(...) returns None, `... or []`/`... or None` falls through to the
default path, and the wizard fields are silently ignored.

This test proves both CONF keys are present in the module-level
_SOLVER_ALL_KEYS tuple that drives what NimbusSolverConfigSensor
publishes -- closing the last gap so #56's fix works end-to-end for
anyone who fills in the wizard.
"""
import unittest

import _solver_path  # noqa: F401
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import (
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
)


class TestSolverConfigSensorPublishesWizardFields(unittest.TestCase):
    """Both new wizard fields (added in commit 02cdae8, wired into the
    writer in a72260d) must be in _SOLVER_ALL_KEYS so
    NimbusSolverConfigSensor publishes them as attributes on
    sensor.nimbus_solver_config -- otherwise fetch_solver_config()
    reads a cfg dict missing them, and the writer's cfg.get(...) reads
    return None on every install regardless of what the user set."""

    def test_load_forecast_entities_is_published(self):
        self.assertIn(CONF_SOLVER_LOAD_FORECAST_ENTITIES, sensor._SOLVER_ALL_KEYS)

    def test_whole_house_cross_check_sensor_is_published(self):
        self.assertIn(CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR, sensor._SOLVER_ALL_KEYS)
