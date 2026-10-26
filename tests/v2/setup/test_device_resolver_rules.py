"""The device resolver behind the two-step setup: which device each role
resolves to, and when it asks (#1574) (#1837).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.append(
    str(Path(__file__).resolve().parents[3] / "custom_components" / "nimbus_load")
)
import device_resolver as dr

START = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
END = START + timedelta(days=1)


def _series(fn, minutes=1440, step=5):
    """kW series from fn(hour_of_day) at `step`-minute changes."""
    return [
        (START + timedelta(minutes=m), fn(m / 60.0)) for m in range(0, minutes, step)
    ]


def _counter_from(series, part=+1):
    """A daily energy counter that genuinely follows `series` (kWh)."""
    total, out = 0.0, []
    for (t, v), (t2, _) in zip(series, [*series[1:], (END, 0.0)], strict=False):
        out.append((t, round(total, 4)))
        x = v if part > 0 else -v
        if x > 0:
            total += x * (t2 - t).total_seconds() / 3600.0
    out.append((END - timedelta(seconds=1), round(total, 4)))
    return out


def solar(h):
    return max(0.0, 8.0 * (1 - abs(h - 12) / 6)) if 6 <= h <= 18 else 0.0


def house(h):
    return 2.0 + (1.5 if 17 <= h <= 22 else 0.0)


def battery(h):  # +discharge evening, -charge midday
    return 6.0 if 17 <= h < 23 else (-5.0 if 9 <= h < 14 else 0.0)


# --- Energy Dashboard preferences ------------------------------------------------


# --- integration, coverage and counters --------------------------------------------


# --- pairing ---------------------------------------------------------------------------


def test_a_direction_the_window_cannot_tell_apart_is_insufficient():
    # test_a_direction_the_window_cannot_tell_apart_is_insufficient: Equal charge and discharge, at the same rate: the sensor matches, but
    sym = _series(lambda h: 4.0 if h < 12 else -4.0)
    m = dr.match_power_sensor(
        {"sensor.sym": sym},
        START,
        END,
        energy_out=48.0,
        energy_in=48.0,
    )
    assert (m.status, m.entity_id, m.sign) == (dr.INSUFFICIENT, None, None)
    assert "direction" in m.reason

    # test_balance_finds_a_watts_sensor_read_as_kw
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance([x * 1000 for x in load], pv, bat, grid)
    assert v.explanation == "scale:load:/1000"

    # test_balance_reports_ambiguity_instead_of_a_cause: Mark, #1590: a residual suggests, it does not uniquely prove. With no
    load = [2.0 + (i % 5) * 0.3 for i in range(24)]
    zeros = [0.0] * 24
    v = dr.check_energy_balance(load, zeros, zeros, [-x for x in load])
    assert v.explanation == dr.AMBIGUOUS
    assert {"flip:grid", "flip:load"} <= set(v.hypotheses)
    assert v.ok is False


# --- the devices check each other (design §3.3) ----------------------------------


def _aligned():
    hours = [h / 4 for h in range(96)]
    load = [house(h) for h in hours]
    pv = [solar(h) for h in hours]
    bat = [battery(h) for h in hours]
    grid = [load[i] - pv[i] - bat[i] for i in range(96)]
    return load, pv, bat, grid
