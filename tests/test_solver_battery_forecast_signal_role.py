"""Real regression guard for nimbus issue #189 (Mark Purcell, real-
install reproducer, the follow-up to #187): sensor.nimbus_solver_
battery_forecast -- the "flagship diagnostic sensor" a Nimbus dashboard
is most likely to be built against -- never got v0.89.1's source_sensor/
signal_role attributes at all. Confirmed root cause: it's the Solver's
own LP-derived plan (a THIRD distinct sensor class/push site, after
NimbusForecastSensor fixed in v0.89.1 and the household-load-total
push fixed in v0.92.1 for #187), so neither earlier fix ever reached it.

Source-inspection style, matching the existing precedent in
tests/test_solver_writer_load_total_state_consistency.py -- main()'s
own real pipeline is too large to mock end-to-end for this; reading the
real, deployed source and asserting the exact push call carries both
keys is a real, if lightweight, guard against a future regression.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


def _extract_battery_forecast_push(src: str) -> str:
    """Return just the ha_post_state(ENTITY_ID, ...) call block that
    publishes sensor.nimbus_solver_battery_forecast, so the assertions
    below can't accidentally match some unrelated push elsewhere in
    this large file."""
    marker = "ha_post_state(\n        ENTITY_ID,"
    start = src.index(marker)
    return src[start : start + 2000]


def test_signal_role_and_source_sensor_are_present():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_battery_forecast_push(src)
    assert '"signal_role": "battery"' in block, (
        "sensor.nimbus_solver_battery_forecast is missing signal_role "
        "-- nimbus issue #189 regression"
    )
    assert re.search(r'"source_sensor":\s*cfg\["solver_battery_soc_sensor"\]', block), (
        "sensor.nimbus_solver_battery_forecast is missing source_sensor "
        "-- nimbus issue #189 regression"
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
