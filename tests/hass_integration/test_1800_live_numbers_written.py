"""nimbus #1800 on real Home Assistant: the wizard's battery and grid answers
are written through each live number entity's own `number.set_value` (found
by unique_id), a placeholder reads as blank, and a missing entity is
reported, not guessed."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.flows import live_numbers


async def test_answers_go_through_number_set_value(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus (1800)", data={})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    cap = reg.async_get_or_create(
        "number",
        DOMAIN,
        f"{entry.entry_id}_solver_battery_capacity_kwh",
        config_entry=entry,
    )
    chg = reg.async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_solver_max_charge_kw", config_entry=entry
    )
    hass.states.async_set(cap.entity_id, "0.1")
    hass.states.async_set(chg.entity_id, "40.0")
    calls: list[ServiceCall] = []

    async def _set(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    hass.services.async_register("number", "set_value", _set)

    assert (
        live_numbers.current_value(hass, entry.entry_id, "solver_battery_capacity_kwh")
        is None
    )
    assert (
        live_numbers.current_value(hass, entry.entry_id, "solver_max_charge_kw") == 40.0
    )

    missing = await live_numbers.async_apply(
        hass,
        entry.entry_id,
        {"solver_battery_capacity_kwh": 119.7, "solver_grid_max_import_kw": 60.0},
    )
    assert missing == ["solver_grid_max_import_kw"]
    assert [(c.data["entity_id"], c.data["value"]) for c in calls] == [
        (cap.entity_id, 119.7)
    ]
    assert hass.states.get(cap.entity_id).state == "119.7"
