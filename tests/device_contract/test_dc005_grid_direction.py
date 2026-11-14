"""The grid's import is the counter that rises at night (reference household,
14 Nov 2026: the Energy Dashboard had import and export reversed, and
discovery proposed them reversed). Synthetic values only."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("device_contract")
_load("device_resolver")
dd = _load("device_discovery")

LOCAL = UTC  # the checks below are written in local time already


def _proposal(imp: str, exp: str) -> dd.Proposal:
    def binding(sid):
        return {
            "source": {"kind": "recorder_statistic", "statistic_id": sid},
            "provenance": {"evidence": []},
        }

    return dd.Proposal(
        document={
            "devices": [
                {
                    "id": "grid",
                    "kind": "grid",
                    "bindings": {
                        "import_energy": binding(imp),
                        "export_energy": binding(exp),
                    },
                }
            ]
        }
    )


def _daily_counter(per_night_kwh: float, nights: int) -> list:
    """A daily counter: reset at midnight, rising per_night_kwh between 01:00
    and 04:30 in half-hour steps (inside the 01:00-05:00 window), flat
    otherwise."""
    out = []
    day0 = datetime(2026, 11, 8, tzinfo=UTC)
    for n in range(nights):
        day = day0 + timedelta(days=n)
        value = 0.0
        out.append((day, value))  # the midnight reset
        steps = 7
        for k in range(steps + 1):
            out.append((day + timedelta(hours=1, minutes=30 * k), value))
            value += per_night_kwh / steps
    return out


def test_import_is_the_counter_that_rises_at_night():
    real_import = _daily_counter(1.5, 5)  # the house imports overnight
    real_export = [(datetime(2026, 11, 8, tzinfo=UTC), 0.0)]  # flat: never recorded
    hist = {"sensor.imp": real_import, "sensor.exp": real_export}

    # Reversed in the Energy Dashboard: swapped, with a finding saying why.
    out = dd.check_grid_direction(
        _proposal("sensor.exp", "sensor.imp"), hist, lambda t: t.astimezone(LOCAL)
    )
    grid = out.document["devices"][0]["bindings"]
    assert grid["import_energy"]["source"]["statistic_id"] == "sensor.imp"
    assert grid["export_energy"]["source"]["statistic_id"] == "sensor.exp"
    (finding,) = out.findings
    assert finding["code"] == dd.F_GRID_REVERSED and finding["nights"] == 5
    assert grid["import_energy"]["provenance"]["evidence"][-1]["kind"] == (
        "night_direction"
    )

    # The right way round: left exactly as it is.
    right = _proposal("sensor.imp", "sensor.exp")
    assert dd.check_grid_direction(right, hist, lambda t: t) is right

    # Too few nights to tell, or a counter with no history: left alone.
    short = {"sensor.imp": _daily_counter(1.5, 2), "sensor.exp": real_export}
    rev = _proposal("sensor.exp", "sensor.imp")
    assert dd.check_grid_direction(rev, short, lambda t: t) is rev
    assert dd.check_grid_direction(rev, {"sensor.imp": real_import}, lambda t: t) is rev

    # A midnight reset is a drop, never counted as a rise.
    rises = dd.night_rise(real_import, lambda t: t)
    assert all(abs(v - 1.5) < 1e-9 for v in rises.values()) and len(rises) == 5
