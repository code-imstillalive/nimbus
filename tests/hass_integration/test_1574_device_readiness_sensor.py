"""nimbus #1574, DC-006 slice 1b on real Home Assistant: readiness per
function, gathered from the install and published as
`sensor.nimbus_device_readiness`. Reads only. Synthetic settings only
(household: "100% safe")."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha
from custom_components.nimbus_load.const import DOMAIN

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

    # test_scoring_reads_the_latest_days_site_exchange_verdict
    # DC-006 slice 5 (DC-R10): readiness takes the latest scored day's
    # verdict from the quality report, the entity the scored days come from;
    # none before a report exists or when it carries no verdict.
    from homeassistant.helpers import entity_registry as er

    assert device_contract_ha._reconciliation(hass, entry) is None
    eid = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_nimbus_solver_quality_report",
            config_entry=entry,
        )
        .entity_id
    )
    hass.states.async_set(
        eid,
        "80",
        {"latest_date": "2026-10-26", "meter_reconciliation": {"status": "disagrees"}},
    )
    assert device_contract_ha._reconciliation(hass, entry) == "disagrees"
    hass.states.async_set(eid, "80", {"latest_date": "2026-10-26"})
    assert device_contract_ha._reconciliation(hass, entry) is None
    assert device_contract_ha._scored_days(hass, entry) == 1
