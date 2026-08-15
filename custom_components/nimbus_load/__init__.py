"""The Nimbus integration -- a self-retraining ML forecaster.

One hub config entry, any number of "load" or "power_signal" subentries
(see config_flow.py / flows/load_subentry.py / flows/signal_subentry.py)
-- each one gets its own NimbusCoordinator, its own persisted model, and
its own forecast sensor + device, so adding one (there can be many --
built for a real 18-circuit-breaker household) never means a full new
integration setup, and each one's health is independently visible in the
device registry. Both subentry types share the exact same coordinator/
forecasting engine -- the only real difference is which config fields
each one's own "+ Add" form asks for (a load's optional schedule/
expected-load fields don't apply to a Battery/Solar/Grid power signal).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL
from .coordinator import NimbusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

_FORECASTABLE_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Nimbus hub -- one coordinator per load/power_signal subentry.

    hass.data[DOMAIN][entry.entry_id] is a dict keyed by subentry_id, not a
    single coordinator -- sensor.py iterates it to create one entity per
    load/signal. A hub with zero subentries yet (right after first install,
    before anything's been added via "+ Add") is valid and simply sets up
    nothing further until the first one exists.
    """
    forecastable_subentries = [
        s for s in entry.subentries.values() if s.subentry_type in _FORECASTABLE_SUBENTRY_TYPES
    ]

    coordinators: dict[str, NimbusCoordinator] = {}
    for subentry in forecastable_subentries:
        coordinator = NimbusCoordinator(hass, entry, subentry)
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
        coordinators[subentry.subentry_id] = coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the whole hub when a load subentry is added, edited, or
    removed. Home Assistant's own subentry flow (async_create_entry /
    async_update_and_abort / subentry deletion) triggers this automatically
    -- this is what makes adding load #2 through #18 take effect immediately
    with no restart, matching HAEO's own hot-add behaviour for its elements.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the Nimbus hub and every one of its load coordinators."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinators: dict[str, NimbusCoordinator] = hass.data[DOMAIN].pop(entry.entry_id, {})
        for coordinator in coordinators.values():
            coordinator.async_unload()
    return unload_ok
