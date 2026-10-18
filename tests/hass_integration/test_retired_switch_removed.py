"""The Bank Solar First switch (v0.94.464) is retired: its registry row is
deleted on setup, so an install that had it is not left with an
"unavailable" switch. Only exact retired unique_ids go; a live switch of the
same entry stays. Real entity registry, because the stub tree has none."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import (
    RETIRED_SWITCH_KEYS,
    _async_remove_retired_switches,
)
from custom_components.nimbus_load.const import DOMAIN


async def test_the_retired_switch_row_goes_and_a_live_one_stays(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus (retired)", data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    retired = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_solver_bank_solar_first",
        config_entry=entry,
    )
    live = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_solver_dispatch_dry_run",
        config_entry=entry,
    )
    assert "solver_bank_solar_first" in RETIRED_SWITCH_KEYS
    _async_remove_retired_switches(hass, entry)
    assert registry.async_get(retired.entity_id) is None
    assert registry.async_get(live.entity_id) is not None
    _async_remove_retired_switches(hass, entry)  # idempotent
