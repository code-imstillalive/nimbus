"""nimbus #1574, DC-005 step 5b on real Home Assistant: history pairing.

The candidates are the power sensors on the same HA device as the energy
counter; a match is a question to confirm, never a binding; no device, no
candidate or an unreadable history is a finding. The recorder read is the
only thing patched. Synthetic only (household: "100% safe")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha
from custom_components.nimbus_load.const import DOMAIN

NOW = datetime(2026, 10, 19, 4, 0, tzinfo=UTC)
START = NOW - timedelta(days=device_contract_ha.PAIRING_DAYS)


def battery(h: float) -> float:  # +discharge evening, -charge midday
    h %= 24
    return 6.0 if 17 <= h < 23 else (-5.0 if 9 <= h < 14 else 0.0)


def _power():
    return [
        (START + timedelta(minutes=m), battery(m / 60.0))
        for m in range(0, device_contract_ha.PAIRING_DAYS * 1440, 5)
    ]


def _counter(series, part):
    total, out = 0.0, []
    for (t, v), (t2, _) in zip(series, [*series[1:], (NOW, 0.0)], strict=False):
        out.append((t, round(total, 4)))
        x = v if part > 0 else -v
        if x > 0:
            total += x * (t2 - t).total_seconds() / 3600.0
    out.append((NOW - timedelta(seconds=1), round(total, 4)))
    return out


POWER = _power()
HISTORY = {
    "sensor.bat_power": POWER,
    "sensor.bat_out_kwh": _counter(POWER, +1),
    "sensor.bat_in_kwh": _counter(POWER, -1),
}
PREFS = {
    "energy_sources": [
        {
            "type": "battery",
            "stat_energy_from": "sensor.bat_out_kwh",
            "stat_energy_to": "sensor.bat_in_kwh",
        }
    ]
}


async def _setup(
    hass: HomeAssistant, *, on_device: bool = True, power_unit: str = "kW"
):
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus (1574 pairing)", data={})
    entry.add_to_hass(hass)
    inverter = MockConfigEntry(domain="some_inverter", data={})
    inverter.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=inverter.entry_id, identifiers={("some_inverter", "inv1")}
    )
    reg = er.async_get(hass)
    for uid, object_id in (
        ("out", "bat_out_kwh"),
        ("in", "bat_in_kwh"),
        ("power", "bat_power"),
        ("temp", "bat_temperature"),
    ):
        reg.async_get_or_create(
            "sensor",
            "some_inverter",
            uid,
            config_entry=inverter,
            device_id=device.id if on_device else None,
            suggested_object_id=object_id,
        )
    hass.states.async_set(
        "sensor.bat_power", "1.5", {"unit_of_measurement": power_unit}
    )
    hass.states.async_set("sensor.bat_temperature", "31", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.bat_out_kwh", "12", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.bat_in_kwh", "10", {"unit_of_measurement": "kWh"})
    return entry


def _patches(history=HISTORY, raises=False):
    async def fake_history(_hass, entity_ids, start, end):
        assert (start, end) == (START, NOW)
        if raises:
            raise RuntimeError("no recorder")
        return {eid: history[eid] for eid in entity_ids if eid in history}

    return (
        patch.object(device_contract_ha, "_async_energy_prefs", return_value=PREFS),
        patch.object(device_contract_ha, "_async_history", side_effect=fake_history),
        patch.object(device_contract_ha.dt_util, "utcnow", return_value=NOW),
    )


async def test_the_same_devices_power_sensor_is_offered_as_a_question(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    prefs, hist, now = _patches()
    with (
        prefs,
        hist as fake,
        now,
        patch("homeassistant.core.ServiceRegistry.async_call") as calls,
    ):
        out = await device_contract_ha.async_discover(hass, entry)
    assert calls.call_count == 0
    # Only power sensors on the counter's device were read: not the
    # temperature beside them.
    (call,) = fake.call_args_list
    assert sorted(call.args[1]) == [
        "sensor.bat_in_kwh",
        "sensor.bat_out_kwh",
        "sensor.bat_power",
    ]
    battery_device = next(d for d in out["document"]["devices"] if d["id"] == "battery")
    assert "power" not in battery_device["bindings"]
    (q,) = [q for q in out["questions"] if q["code"] == "confirm_history_match"]
    (c,) = q["candidates"]
    assert c["source"] == "sensor.bat_power"
    assert c["binding_source"]["registry_id"]
    assert c["normalization"]["native_positive_direction"] == "discharge"


async def test_a_counter_on_no_device_is_a_finding(hass: HomeAssistant, hass_storage):
    entry = await _setup(hass, on_device=False)
    prefs, hist, now = _patches()
    with prefs, hist as fake, now:
        out = await device_contract_ha.async_discover(hass, entry)
    assert fake.call_count == 0
    assert [f["code"] for f in out["findings"] if f.get("device") == "battery"] == [
        "history_pairing_no_device"
    ]


async def test_an_unreadable_history_is_a_finding_not_a_guess(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    prefs, hist, now = _patches(raises=True)
    with prefs, hist, now:
        out = await device_contract_ha.async_discover(hass, entry)
    assert not [q for q in out["questions"] if q["code"] == "confirm_history_match"]
    assert [f["code"] for f in out["findings"]].count(
        "history_pairing_history_unreadable"
    ) == 1


async def test_no_power_sensor_beside_the_counter_is_a_finding(
    hass: HomeAssistant, hass_storage
):
    """Mark Purcell, #1892 review: `history_pairing_no_candidates` had no
    real-HA test. With the battery's power sensor reading in kWh it is not a
    power sensor, and the temperature beside it is not either: nothing to
    pair, said plainly, and no history read."""
    entry = await _setup(hass, power_unit="kWh")
    prefs, hist, now = _patches()
    with prefs, hist as fake, now:
        out = await device_contract_ha.async_discover(hass, entry)
    assert fake.call_count == 0
    assert [f["code"] for f in out["findings"] if f.get("device") == "battery"] == [
        "history_pairing_no_candidates"
    ]
    assert not [q for q in out["questions"] if q["code"] == "confirm_history_match"]
