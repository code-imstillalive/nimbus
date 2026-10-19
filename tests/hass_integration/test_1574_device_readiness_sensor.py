"""nimbus #1574, DC-006 slice 1b on real Home Assistant: readiness per
function, gathered from the install and published as
`sensor.nimbus_device_readiness`. Reads only. Synthetic settings only
(household: "100% safe")."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha, device_storage
from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.readiness_sensor import (
    NimbusDeviceReadinessSensor,
)

OPTIONS = {
    "grid_sensor": "sensor.meter_power",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
}


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    hass.config.currency = "AUD"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus (1574 readiness)",
        data={},
        options=options or OPTIONS,
    )
    entry.add_to_hass(hass)
    return entry


async def test_legacy_reads_todays_settings_and_writes_nothing(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    hass.states.async_set("sensor.meter_power", "1.2", {"unit_of_measurement": "kW"})
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        out = await device_contract_ha.async_readiness(hass, entry)
    assert calls.call_count == 0
    assert device_storage.store_key(entry.entry_id) not in hass_storage
    assert (out["mode"], out["revision"]) == ("legacy", 0)
    fn = out["functions"]
    assert set(fn) == {"observe", "forecast", "plan", "score"}
    # Grid power is bound and reads: observe is ready.
    assert fn["observe"]["state"] == "ready"
    # No load is measured in these settings: forecasting needs one, and the
    # plan names it rather than reading as zero.
    assert fn["forecast"]["state"] == "needs"
    assert fn["plan"]["state"] == "needs"
    assert out["ready"] == 1


async def test_a_missing_price_sensor_is_data_not_a_need(
    hass: HomeAssistant, hass_storage
):
    """The price roles are bound; their entities do not exist yet. That is
    the data's state, listed, never a reason the plan "needs" a price."""
    entry = await _setup(hass)
    hass.states.async_set("sensor.meter_power", "1.2", {"unit_of_measurement": "kW"})
    out = await device_contract_ha.async_readiness(hass, entry)
    missing = [(m["device"], m["role"]) for m in out["functions"]["plan"]["missing"]]
    assert ("grid", "import_price") not in missing
    assert ("grid", "export_price") not in missing


async def test_the_sensor_publishes_the_count_and_each_function(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    hass.states.async_set("sensor.meter_power", "1.2", {"unit_of_measurement": "kW"})
    sensor = NimbusDeviceReadinessSensor(entry, "test")
    sensor.hass = hass
    await sensor._async_refresh()
    assert sensor.native_value == 1
    attrs = sensor.extra_state_attributes
    assert attrs["of"] == 4 and attrs["mode"] == "legacy"
    assert attrs["observe_state"] == "ready"
    assert set(attrs["functions"]) == {"observe", "forecast", "plan", "score"}
    assert sensor.unique_id == f"{entry.entry_id}_device_readiness"
    assert "functions" in sensor._unrecorded_attributes
