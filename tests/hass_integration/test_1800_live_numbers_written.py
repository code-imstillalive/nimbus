"""nimbus #1800 on real Home Assistant: the wizard's battery and grid answers
are written through the Nimbus number entity's own async_set_native_value,
found by unique_id in the real entity registry, and only when the registry
says the entity belongs to nimbus_load. A placeholder reads as blank; an
entity that is missing, not loaded or another integration's is skipped,
never written (DC-R15: setup never operates equipment)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.flows import live_numbers


class _Entity:
    def __init__(self) -> None:
        self.written: list[float] = []

    async def async_set_native_value(self, value: float) -> None:
        self.written.append(value)


class _Component:
    def __init__(self, entities: dict[str, _Entity]) -> None:
        self._entities = entities

    def get_entity(self, entity_id: str):
        return self._entities.get(entity_id)


async def test_only_a_loaded_nimbus_entity_is_written(hass: HomeAssistant):
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
    # Same unique_id shape, another integration: must never be written.
    foreign = reg.async_get_or_create(
        "number", "sungrow", f"{entry.entry_id}_solver_max_discharge_kw"
    )
    hass.states.async_set(cap.entity_id, "0.1")
    hass.states.async_set(chg.entity_id, "40.0")
    cap_entity, foreign_entity = _Entity(), _Entity()
    hass.data.setdefault("entity_components", {})["number"] = _Component(
        {cap.entity_id: cap_entity, foreign.entity_id: foreign_entity}
    )

    assert (
        live_numbers.current_value(hass, entry.entry_id, "solver_battery_capacity_kwh")
        is None
    )
    assert (
        live_numbers.current_value(hass, entry.entry_id, "solver_max_charge_kw") == 40.0
    )

    skipped = await live_numbers.async_apply(
        hass,
        entry.entry_id,
        {
            "solver_battery_capacity_kwh": 119.7,
            "solver_max_charge_kw": 40.0,  # registered but not loaded
            "solver_max_discharge_kw": 40.0,  # only another integration's
            "solver_grid_max_import_kw": 60.0,  # not registered
        },
    )
    assert cap_entity.written == [119.7]
    assert foreign_entity.written == []
    assert sorted(skipped) == [
        "solver_grid_max_import_kw",
        "solver_max_charge_kw",
        "solver_max_discharge_kw",
    ]
