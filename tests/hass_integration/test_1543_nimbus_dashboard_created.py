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
        "Solver",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    assert all("icon" not in v for v in config["views"])
    # nimbus #1594: the Solver tab is written with this install's entity_ids,
    # never with the template's `@key` placeholders.
    import json

    assert '"@' not in json.dumps(config["views"][1])

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


async def _reload_from_storage(hass: HomeAssistant, url: str) -> None:
    """Replace the cached dashboard with a fresh reader over HA's real
    Lovelace Store, as on the next HA start: what comes back has been
    through storage, not handed back as the same Python object."""
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    data = hass.data[LOVELACE_DATA]
    data.dashboards[url] = LovelaceStorage(hass, data.dashboards[url].config)


async def test_untouched_and_edited_survive_a_real_storage_round_trip(
    hass: HomeAssistant, monkeypatch
):
    """Mark's review of #1595: "untouched" is decided by comparing the
    fingerprint taken at write time with what Lovelace storage hands back
    on a later run. A false "untouched" would overwrite a household's
    own edit, so drive write -> reload -> edit -> reload -> redesign
    through real HA storage."""
    from custom_components.nimbus_load import forecaster_dashboard as fd

    await _setup_nimbus(hass)
    url = "dashboard-nimbus"
    await _reload_from_storage(hass, url)
    # Untouched tabs read back from storage still match what was written.
    assert await async_ensure_nimbus_dashboard(hass) == []

    # The household edits the Topology tab (adds a card), saved through HA.
    dash = hass.data[LOVELACE_DATA].dashboards[url]
    config = await dash.async_load(False)
    topology = next(v for v in config["views"] if v["path"] == "topology")
    topology["sections"][0]["cards"].append({"type": "markdown", "content": "mine"})
    await dash.async_save(config)
    await _reload_from_storage(hass, url)

    # A release redesigns both Topology and Regret.
    specs = []
    for spec in fd.STANDARD_VIEWS:
        if spec["key"] in ("topology", "regret"):
            spec = {**spec, "cards": ({"redesigned": True},)}
        specs.append(spec)
    monkeypatch.setattr(fd, "STANDARD_VIEWS", tuple(specs))
    done = await async_ensure_nimbus_dashboard(hass)
    assert sorted(done) == [
        "beside:topology@dashboard-nimbus",
        "updated:regret@dashboard-nimbus",
    ]

    await _reload_from_storage(hass, url)
    views = (await hass.data[LOVELACE_DATA].dashboards[url].async_load(False))["views"]
    titles = [v["title"] for v in views]
    assert titles == [
        "Forecaster",
        "Solver",
        "Topology",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    edited = views[2]
    assert edited["sections"][0]["cards"][-1] == {"type": "markdown", "content": "mine"}
    assert views[3]["path"] == "topology-2"
    assert views[3]["sections"][0]["cards"][0]["redesigned"] is True
    assert views[5]["sections"][0]["cards"][0]["redesigned"] is True
