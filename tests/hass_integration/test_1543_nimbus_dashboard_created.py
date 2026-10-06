"""nimbus #1543 against a real Home Assistant: a new install's Nimbus
dashboard is created through the lovelace component's own dashboards
collection, so it appears in the UI's dashboard list (which a second
collection instance would not achieve) and holds the four standard tabs.

This is the one part of forecaster_dashboard.py the stub suite cannot prove:
that `hass.data["websocket_api"]["lovelace/dashboards/create"]` really
unwraps to the collection on the pinned HA version.
"""

from __future__ import annotations

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.forecaster_dashboard import (
    _dashboards_collection,
    async_ensure_nimbus_dashboard,
)


async def _setup_nimbus(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus", data={}, options={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_dashboards_collection_is_found_on_real_ha(hass: HomeAssistant):
    await _setup_nimbus(hass)
    assert _dashboards_collection(hass) is not None


async def test_a_new_install_gets_the_nimbus_dashboard(
    hass: HomeAssistant, hass_ws_client
):
    await _setup_nimbus(hass)

    dashboard = hass.data[LOVELACE_DATA].dashboards.get("dashboard-nimbus")
    assert dashboard is not None
    config = await dashboard.async_load(False)
    assert [v["title"] for v in config["views"]] == [
        "Forecaster",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    assert all("icon" not in v for v in config["views"])

    # Listed where the UI lists dashboards: created through HA's own collection.
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "lovelace/dashboards/list"})
    msg = await client.receive_json()
    assert msg["success"]
    listed = {d["url_path"]: d for d in msg["result"]}
    assert listed["dashboard-nimbus"]["title"] == "Nimbus"
    assert (
        "icon" not in listed["dashboard-nimbus"]
        or not listed["dashboard-nimbus"]["icon"]
    )

    # A second start changes nothing.
    assert await async_ensure_nimbus_dashboard(hass) == []
