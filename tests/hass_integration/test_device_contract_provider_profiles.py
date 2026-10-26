"""Price providers found by their entities' built-in unique ids (#1837: the
coverage-by-risk map found `device_contract_ha.profile_instances` and its
per-provider key parsing untested).

Each configured provider instance is read from the real entity registry,
by unique id, so a renamed entity is still found. What this pins, per
provider (DC-005's discovery input, device_discovery.PROFILES):

- LocalVolts: `<config entry id>_<key>`; a unique id without that prefix is
  not one of its keys.
- Amber Express: `<site>_general_price` / `<site>_feed_in_price`; any other
  suffix is not a price.
- Amber Electric: `<site>-forecasts-<channel>`; nothing else is a forecast.
- A reported-only provider (AEMO NEM): one instance per config entry, no keys.
- A disabled entity, and a platform that is no price provider, are ignored.

Synthetic ids only (household: "100% safe").
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha


def _entry(hass: HomeAssistant, domain: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, title=domain, data={})
    entry.add_to_hass(hass)
    return entry


async def test_each_provider_instance_is_found_by_unique_id(hass: HomeAssistant):
    reg = er.async_get(hass)
    lv = _entry(hass, "localvolts_v2")
    amber = _entry(hass, "amber_express")
    ae = _entry(hass, "amberelectric")
    aemo = _entry(hass, "aemo_nem")
    met = _entry(hass, "met")

    def add(platform, uid, entry, object_id, **kw):
        return reg.async_get_or_create(
            "sensor",
            platform,
            uid,
            config_entry=entry,
            suggested_object_id=object_id,
            **kw,
        ).entity_id

    lv_buy = add("localvolts_v2", f"{lv.entry_id}_buy_flex_up", lv, "renamed_buy")
    add("localvolts_v2", "not_this_entrys_key", lv, "lv_stray")
    general = add("amber_express", "site1_general_price", amber, "amber_general")
    feed_in = add("amber_express", "site1_feed_in_price", amber, "amber_feed_in")
    add("amber_express", "site1_renewables", amber, "amber_renewables")
    add(
        "amber_express",
        "site2_general_price",
        amber,
        "amber_disabled",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    forecast = add("amberelectric", "01ABC-forecasts-general", ae, "ae_forecast")
    add("amberelectric", "01ABC-general", ae, "ae_general")
    add("aemo_nem", "qld1_price", aemo, "aemo_qld")
    add("met", "home", met, "weather_home")

    assert device_contract_ha.profile_instances(hass) == [
        {"profile": "aemo_nem", "instance": aemo.entry_id, "keys": {}},
        {
            "profile": "amber_express",
            "instance": "site1",
            "keys": {"general_price": general, "feed_in_price": feed_in},
        },
        {
            "profile": "amberelectric",
            "instance": "01ABC",
            "keys": {"forecasts-general": forecast},
        },
        {
            "profile": "localvolts_v2",
            "instance": lv.entry_id,
            "keys": {"buy_flex_up": lv_buy},
        },
    ]
