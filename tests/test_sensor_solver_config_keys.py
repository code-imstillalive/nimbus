"""Real regression test for a real, live-found bug (2026-08-23): a Solver
settings wizard field can be genuinely, correctly SAVED into
entry.options by flows/hub_options.py (present in _SOLVER_WIZARD_SCHEMA_
KEYS) while NimbusSolverConfigSensor (sensor.py) silently never exposes
it at all, because it was never added to _SOLVER_ALL_KEYS. Since
nimbus_solver_forecast_writer.py's fetch_solver_config() reads config
ONLY through this sensor's own attributes (config_entries.options isn't
exposed over HA's plain REST API -- see that function's own docstring),
a field missing from _SOLVER_ALL_KEYS is invisible to the writer no
matter how many times a household resubmits the wizard. Real, live
symptom this caused: solver_load_forecast_entities/solver_whole_house_
cross_check_sensor both showed correctly pre-filled in the wizard on
reopen (proof entry.options genuinely had the data) while sensor.
nimbus_solver_config's own attributes showed them as None/missing,
every single time, across multiple genuine resubmissions.

This test asserts every key flows/hub_options.py's wizard can actually
SAVE also appears in sensor.py's own _SOLVER_ALL_KEYS -- so a future
field added to one but not the other fails a test immediately, instead
of silently reproducing this exact multi-hour live debugging session.

Imports and exercises the REAL constants (not a reimplementation)
against tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor  # noqa: E402
from custom_components.nimbus_load.const import (  # noqa: E402
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
)
from custom_components.nimbus_load.flows.hub_options import _SOLVER_WIZARD_SCHEMA_KEYS  # noqa: E402


def test_every_wizard_saveable_key_is_exposed_by_the_bridge_sensor():
    missing = [
        key for key in _SOLVER_WIZARD_SCHEMA_KEYS if key not in sensor._SOLVER_ALL_KEYS
    ]
    assert missing == [], (
        f"{missing} can be saved by the Solver wizard but are never exposed by "
        "NimbusSolverConfigSensor -- fetch_solver_config() (the writer script's "
        "only channel to read config) can never see these fields regardless of "
        "how many times the wizard is resubmitted. Add them to sensor.py's own "
        "_SOLVER_ALL_KEYS."
    )


def test_the_two_specific_fields_from_the_real_2026_08_23_incident_are_present():
    assert CONF_SOLVER_LOAD_FORECAST_ENTITIES in sensor._SOLVER_ALL_KEYS
    assert CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR in sensor._SOLVER_ALL_KEYS


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
