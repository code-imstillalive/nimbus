"""Real regression test for issue #99: NimbusForecastSensor (the
subentry-published load/signal forecast class) was missed by PR #77's
Recorder 16 KB attribute-cap fix -- that PR added
`_unrecorded_attributes = frozenset({"forecast"})` to
_NimbusSolverPushSensor (see test_sensor_solver_push_entities.py's own
matching class-attribute checks) but never to this class, so any
subentry's own real forecast sensor (a household's 18 loads, or Mark
Purcell's own confirmed-live #99 report showing this warning firing
hundreds of times for his signal forecasts) kept tripping the
Recorder's "exceeds maximum size of 16384 bytes" warning #77 was meant
to close everywhere.

Class-attribute check only, same minimal pattern as the sibling test
file's own -- no instance construction needed, _unrecorded_attributes
is read directly off the class.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor


def test_forecast_sensor_has_unrecorded_attributes_for_recorder_cap():
    cls = sensor.NimbusForecastSensor
    # Recorder 16 KB attribute cap fix (#59/#99): the forecast list is a
    # projection, not a historical fact, so it's excluded from
    # long-term storage -- same reasoning, same fix, as the solver-push
    # sensors already carry.
    assert cls._unrecorded_attributes == frozenset({"forecast"})


def test_health_report_sensor_has_unrecorded_attributes_for_recorder_cap():
    # Same class of bug as #99 above, found live on devhub (2026-09-01):
    # NimbusHealthReportSensor's extra_state_attributes() returns up to
    # 20 recent_errors + 20 recent_warnings + one subentry_status entry
    # per forecastable subentry -- real, current diagnostic state, not
    # a historical fact worth keeping in long-term stats, and large
    # enough to trip the Recorder's 16 KB cap on nearly every cycle
    # (confirmed live: 62 warnings in one 30-minute window). This class
    # was never given the same _unrecorded_attributes treatment #99
    # gave NimbusForecastSensor -- fixed the same way.
    cls = sensor.NimbusHealthReportSensor
    assert cls._unrecorded_attributes == frozenset(
        {"recent_errors", "recent_warnings", "subentry_status"}
    )


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
