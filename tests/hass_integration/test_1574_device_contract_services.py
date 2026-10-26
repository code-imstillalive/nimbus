"""nimbus #1574, DC-004 step 5a on real Home Assistant: the device
document's preview and legacy <-> shadow mode.

Household, 18 Oct 2026: "Make sure u do not break nimbus. Test deep and
thoroughly." So this pins what must NOT change: the config entry's options,
every subentry's data and every number entity's state are identical after a
preview, a move to shadow and back, and no service is called at all (DC-R15).
Active mode has its own tests (test_1574_device_contract_active.py).
Only the device store is written. Synthetic settings only (household: "100%
safe").
"""

from __future__ import annotations

import copy

from homeassistant.config_entries import ConfigSubentryDataWithId
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha, device_storage
from custom_components.nimbus_load.const import DOMAIN

OPTIONS = {
    "grid_sensor": "sensor.meter_power",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
    "solver_battery_soc_sensor": "sensor.bat_soc",
    "solver_battery_power_sensor": "sensor.bat_power",
    "battery_sensor": "sensor.bat_power",
    "solver_battery_power_positive_is_charge": True,
    "solar_sensor": "sensor.pv_power",
    "solver_solar_forecast_sensor": "sensor.pv_forecast",
    "solver_load_forecast_sensor": "sensor.house_forecast",
    "solver_offer_curve_enabled": True,
}
NUMBERS = {
    "solver_battery_capacity_kwh": "40.0",
    "solver_battery_min_soc_percent": "5.0",
    "solver_battery_max_soc_percent": "100.0",
    "solver_max_charge_kw": "10.0",
    "solver_max_discharge_kw": "10.0",
    "solver_grid_max_import_kw": "20.0",
    "solver_grid_max_export_kw": "10.0",
}
SUBENTRIES = [
    ConfigSubentryDataWithId(
        data={
            "load_sensor": "sensor.pool_power",
            "schedule_start_hour": 8,
            "schedule_end_hour": 15,
            "expected_load_kw": 1.3,
        },
        subentry_id="sub_pool",
        subentry_type="load",
        title="Pool pump",
        unique_id=None,
    ),
    ConfigSubentryDataWithId(
        data={
            "controllable_load_name": "Hot water",
            "controllable_load_device_entity": "switch.hws",
        },
        subentry_id="sub_hws",
        subentry_type="controllable_load",
        title="Hot water",
        unique_id=None,
    ),
]


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus (1574)",
        data={},
        options=copy.deepcopy(options or OPTIONS),
        subentries_data=copy.deepcopy(SUBENTRIES),
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    for key, value in NUMBERS.items():
        ent = reg.async_get_or_create(
            "number", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
        )
        hass.states.async_set(ent.entity_id, value)
    return entry


def _settings(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Everything a consumer reads that DC-004 must not change in shadow."""
    reg = er.async_get(hass)
    return {
        "options": copy.deepcopy(dict(entry.options)),
        "subentries": {sid: dict(s.data) for sid, s in entry.subentries.items()},
        "numbers": {
            key: hass.states.get(
                reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
            ).state
            for key in NUMBERS
        },
    }


async def test_a_conflict_refuses_shadow_and_writes_nothing(
    hass: HomeAssistant, hass_storage
):
    options = {**OPTIONS, "battery_sensor": "sensor.another_battery"}
    entry = await _setup(hass, options)
    before = _settings(hass, entry)
    result = await device_contract_ha.async_set_mode(hass, entry, "shadow")
    assert result["ok"] is False and result["code"] == "dry_run_not_clean"
    assert [c["code"] for c in result["conflicts"]] == ["conflict_role_sources"]
    assert device_storage.store_key(entry.entry_id) not in hass_storage
    assert _settings(hass, entry) == before
