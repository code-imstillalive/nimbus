"""DC-005 step 3: history pairing for a device with energy but no power
(device_discovery.add_history_pairing). Synthetic histories only."""

from __future__ import annotations

import copy
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


dc = _load("device_contract")
dr = _load("device_resolver")
dd = _load("device_discovery")

START = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
END = START + timedelta(days=1)


def _series(fn, step=5):
    return [(START + timedelta(minutes=m), fn(m / 60.0)) for m in range(0, 1440, step)]


def _counter(series, part=+1):
    """A daily energy counter that genuinely follows `series` (kWh)."""
    total, out = 0.0, []
    for (t, v), (t2, _) in zip(series, [*series[1:], (END, 0.0)], strict=False):
        out.append((t, round(total, 4)))
        x = v if part > 0 else -v
        if x > 0:
            total += x * (t2 - t).total_seconds() / 3600.0
    out.append((END - timedelta(seconds=1), round(total, 4)))
    return out


def battery(h):  # +discharge evening, -charge midday
    return 6.0 if 17 <= h < 23 else (-5.0 if 9 <= h < 14 else 0.0)


def solar(h):
    return max(0.0, 8.0 * (1 - abs(h - 12) / 6)) if 6 <= h <= 18 else 0.0


def house(h):
    return 2.0 + (1.5 if 17 <= h <= 22 else 0.0)


PREFS = {
    "energy_sources": [
        {
            "type": "battery",
            "stat_energy_from": "sensor.bat_out_kwh",
            "stat_energy_to": "sensor.bat_in_kwh",
        },
        {
            "type": "solar",
            "stat_energy_from": "sensor.pv_kwh",
            "stat_rate": "sensor.pv_w",
        },
    ]
}
BAT = _series(battery)
STATS = {
    "sensor.bat_out_kwh": _counter(BAT, +1),
    "sensor.bat_in_kwh": _counter(BAT, -1),
}


def _proposal():
    return dd.propose_from_energy_dashboard(PREFS)


def test_dc_r13_f06_two_equal_readings_are_asked_with_both():
    p = dd.add_history_pairing(
        _proposal(),
        {"battery": {"sensor.bat_a": BAT, "sensor.bat_b": list(BAT)}},
        STATS,
        START,
        END,
    )
    (q,) = [q for q in p.questions if q.get("role") == "power"]
    assert q["code"] == "several_sources"
    assert sorted(c["source"] for c in q["candidates"]) == [
        "sensor.bat_a",
        "sensor.bat_b",
    ]
    assert not [q for q in p.questions if q["code"] == "confirm_history_match"]


def test_no_match_and_too_little_history_are_findings_not_zeros():
    p = dd.add_history_pairing(
        _proposal(), {"battery": {"sensor.house": _series(house)}}, STATS, START, END
    )
    assert not [q for q in p.questions if q.get("role") == "power"]
    (f,) = [f for f in p.findings if f["code"] == "history_pairing_inconclusive"]
    assert f["status"] in (dr.NO_MATCH, dr.INSUFFICIENT)

    short = {"battery": {"sensor.bat_power": BAT[-12:]}}
    p = dd.add_history_pairing(_proposal(), short, STATS, START, END)
    (f,) = [f for f in p.findings if f["code"] == "history_pairing_inconclusive"]
    assert f["status"] == dr.INSUFFICIENT


def test_no_counter_history_is_said_plainly():
    p = dd.add_history_pairing(
        _proposal(), {"battery": {"sensor.bat_power": BAT}}, {}, START, END
    )
    (f,) = [f for f in p.findings if f["code"] == "history_pairing_inconclusive"]
    assert f["reason"] == "no counter history to compare against"


def test_nothing_offered_means_nothing_said_and_input_untouched():
    base = _proposal()
    before = copy.deepcopy(base.document)
    p = dd.add_history_pairing(base, {}, STATS, START, END)
    assert (p.questions, p.findings) == (base.questions, base.findings)
    assert base.document == before


def test_a_solar_device_with_power_already_is_never_paired():
    p = dd.add_history_pairing(
        _proposal(),
        {"solar": {"sensor.other": _series(solar)}},
        {"sensor.pv_kwh": _counter(_series(solar))},
        START,
        END,
    )
    assert not [q for q in p.questions if q.get("device") == "solar"]
