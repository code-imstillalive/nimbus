"""`nimbus_load.get_dashboard` on a real Home Assistant (#1837: the
coverage-by-risk map found `forecaster_dashboard.py`'s install-bound half
untested after 0.94.490 moved it behind this action).

The action returns this install's standard dashboard and writes nothing
(hacs/default#12099). These tests call it the way a household does and read
what comes back: every placeholder resolved against the real entity
registry, the mushroom slider only where mushroom is installed, a moved
setting's time entity rather than its stale number row, and, with no Nimbus
entry to resolve against, the two install-bound tabs left out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import forecaster_dashboard as dash
from custom_components.nimbus_load.const import DOMAIN

STANDARD = ["Forecaster", "Solver", "Topology", "Control Panel", "Regret", "Help"]
MUSHROOM = "custom:mushroom-number-card"


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (get dashboard)", data={}, options={}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _get(hass: HomeAssistant) -> dict:
    return await hass.services.async_call(
        DOMAIN, "get_dashboard", {}, blocking=True, return_response=True
    )


def _resources(*urls: str):
    return SimpleNamespace(async_items=lambda: [{"url": u} for u in urls])


async def test_the_action_returns_every_tab_resolved_against_this_install(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    board = await _get(hass)
    assert [v["title"] for v in board["views"]] == STANDARD
    text = json.dumps(board)
    assert "${@" not in text, "a placeholder was left unresolved"
    assert MUSHROOM not in text  # no mushroom resource here: HA's tile slider
    registry = er.async_get(hass)
    ours = {
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, nimbus_entry.entry_id)
    }
    resolved = sorted(e for e in ours if e in text)
    assert resolved, "no entity of this install reached the dashboard"


@pytest.mark.parametrize(
    "lovelace",
    [
        SimpleNamespace(
            resources=_resources("/hacsfiles/lovelace-mushroom/mushroom.js")
        ),
        {"resources": _resources("/local/mushroom.js?v=4")},
    ],
    ids=["object", "dict"],
)
async def test_the_solver_tab_uses_mushroom_only_where_it_is_installed(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry, monkeypatch, lovelace
):
    monkeypatch.setitem(hass.data, "lovelace", lovelace)
    assert MUSHROOM in json.dumps(await _get(hass))


async def test_an_unreadable_resource_list_means_the_tile_slider(hass: HomeAssistant):
    class Broken:
        def async_items(self):
            raise RuntimeError("storage not loaded")

    hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=Broken())})
    assert dash._has_lovelace_resource(hass, "mushroom") is False
    assert dash._has_lovelace_resource(SimpleNamespace(data={}), "mushroom") is False


async def test_a_moved_settings_stale_number_row_never_wins_over_its_time_entity(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """The reserve window moved number -> time on 11 Oct 2026. A number row
    left under the same unique id is skipped, so the tab names the time
    entity, not one about to be deleted."""
    registry = er.async_get(hass)
    key = "solver_battery_reserve_start_hour"
    stale = registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{nimbus_entry.entry_id}_{key}",
        config_entry=nimbus_entry,
        suggested_object_id="nimbus_stale_reserve_start",
    )
    text = json.dumps(await _get(hass))
    assert f"time.nimbus_{key}" in text
    assert stale.entity_id not in text


async def test_with_no_nimbus_entry_the_install_bound_tabs_are_left_out(
    hass: HomeAssistant,
):
    board = await dash.async_dashboard_config(hass)
    assert [v["title"] for v in board["views"]] == [
        t for t in STANDARD if t not in ("Solver", "Help")
    ]
