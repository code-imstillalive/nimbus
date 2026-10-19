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


def test_only_devices_with_energy_and_no_power_are_targets():
    assert dd.pairing_targets(_proposal()) == {
        "battery": ("sensor.bat_out_kwh", "sensor.bat_in_kwh")
    }


def test_a_clean_match_is_a_question_to_confirm_never_a_binding():
    """History may not decide alone (DC-005's evidence table)."""
    cands = {
        "battery": {
            "sensor.bat_power": BAT,
            "sensor.house_power": _series(house),
        }
    }
    p = dd.add_history_pairing(
        _proposal(), cands, STATS, START, END, registry_ids={"sensor.bat_power": "r1"}
    )
    battery_device = next(d for d in p.document["devices"] if d["id"] == "battery")
    assert "power" not in battery_device["bindings"]
    (q,) = [q for q in p.questions if q["code"] == "confirm_history_match"]
    assert (q["device"], q["role"]) == ("battery", "power")
    (c,) = q["candidates"]
    assert c["binding_source"] == {
        "kind": "entity_state",
        "entity_id": "sensor.bat_power",
        "registry_id": "r1",
    }
    assert c["normalization"] == {
        "positive_direction": "discharge",
        "native_positive_direction": "discharge",
    }
    (ev,) = c["evidence"]
    assert ev["kind"] == "history_pairing" and ev["status"] == "candidate"
    assert ev["error"] < dr.MATCH_TOLERANCE


def test_a_flipped_sensor_is_proposed_with_its_native_sign():
    flipped = [(t, -v) for t, v in BAT]
    p = dd.add_history_pairing(
        _proposal(), {"battery": {"sensor.inv_bat": flipped}}, STATS, START, END
    )
    (q,) = [q for q in p.questions if q["code"] == "confirm_history_match"]
    assert q["candidates"][0]["normalization"] == {
        "positive_direction": "discharge",
        "native_positive_direction": "charge",
    }
    # DC-001 accepts it: the declared positive direction is canonical.
    doc = copy.deepcopy(p.document)
    battery_device = next(d for d in doc["devices"] if d["id"] == "battery")
    battery_device["bindings"]["power"] = {
        "source": q["candidates"][0]["binding_source"],
        "normalization": q["candidates"][0]["normalization"],
    }
    assert not [x for x in dc.validate(doc) if x.code == "sign_not_canonical"]


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
