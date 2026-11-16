"""#2513: a Marketplace install on HA 2026.11.1 showed no Nimbus card until each
card was added under Settings -> Dashboards -> Resources by hand (reported by
Justin, 17 Nov 2026). Loaded only as Home Assistant's extra module, a card can
lose the race against a dashboard built a moment earlier. Every card is now a
dashboard resource, as a HACS card is; one added by hand is brought to the
current version rather than added twice; a YAML-mode dashboard, which has no
resource store, still gets the extra module.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.nimbus_load import frontend


def _card_paths() -> set[str]:
    return {f"/nimbus_load/{c.filename}" for c in frontend._CARDS}


async def _resources(hass: HomeAssistant):
    resources = hass.data["lovelace"].resources
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True
    return resources


async def test_every_card_is_a_dashboard_resource_and_a_hand_added_one_is_updated(
    hass: HomeAssistant,
):
    assert await async_setup_component(hass, "lovelace", {})
    resources = await _resources(hass)
    # The workaround Justin applied: one card added by hand, an old version.
    await resources.async_create_item(
        {"res_type": "module", "url": "/nimbus_load/nimbus-flow-card.js?v=0.105.0"}
    )

    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await frontend.async_register_frontend(hass, "9.9.9")
        await frontend.async_register_frontend(hass, "9.9.9")  # a reload adds nothing

    items = [dict(i) for i in resources.async_items()]
    paths = [str(i["url"]).split("?")[0] for i in items]
    assert sorted(paths) == sorted(_card_paths())  # each card once, none twice
    assert all(i["res_type"] == "module" for i in items)
    assert all("?v=9.9.9-" in str(i["url"]) for i in items)
    add_js.assert_not_called()


async def test_yaml_mode_dashboards_still_get_the_extra_module(hass: HomeAssistant):
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "yaml"}})
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await frontend.async_register_frontend(hass, "9.9.9")
    urls = {c.args[1].split("?")[0] for c in add_js.call_args_list}
    assert urls == _card_paths()
