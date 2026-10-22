"""Every external source Nimbus finds for itself, found one way: through the
entity registry, by the integration (config entry domain) and the entity's
built-in unique_id. Never by entity_id, which a household can rename and a
non-English install names differently (nimbus #1994).

`pricing_autodetect.py` found LocalVolts v2, AEMO NEM Data and NEM PD7DAY this
way first (#1550, #1578). Solar auto-include, the Solcast running-low gate and
LocalVolts' price freshness matched fixed entity_ids instead, so a rename lost
them silently: with no other solar source configured, every plan ran on 0 kW
solar. They now read what `resolve()` finds, published on
`sensor.nimbus_solver_config` as `known_sources`, and nothing in the
integration matches an external entity_id literal.

Unique ids, from each integration's own sensor code:

* Solcast PV Forecast (`solcast_solar`): the sensor key itself,
  `total_kwh_forecast_today` and `total_kwh_forecast_tomorrow`.
* Open-Meteo Solar Forecast (`open_meteo_solar_forecast`):
  `<config entry id>_<key>`, keys `energy_production_today`, `_tomorrow` and
  `_d2` to `_d7`. One entry per site; with several the site is ambiguous and
  none is read (a debug line says so), as `pricing_autodetect` does for
  several LocalVolts accounts.
* LocalVolts v2 (`localvolts_v2`): `<config entry id>_<key>`; the two
  current-rate sensors (`current_buy_rate`, `current_sell_rate`) carry the
  refresh stamp a price's freshness is read from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

SOLCAST_DOMAIN = "solcast_solar"
SOLCAST_KEYS = ("total_kwh_forecast_today", "total_kwh_forecast_tomorrow")

OPEN_METEO_DOMAIN = "open_meteo_solar_forecast"
OPEN_METEO_KEYS = (
    "energy_production_today",
    "energy_production_tomorrow",
    "energy_production_d2",
    "energy_production_d3",
    "energy_production_d4",
    "energy_production_d5",
    "energy_production_d6",
    "energy_production_d7",
)

LV_V2_DOMAIN = "localvolts_v2"
LV_V2_STAMP_KEYS = ("current_buy_rate", "current_sell_rate")

#: What `resolve()` returns when nothing is installed: the shape every reader
#: of `cfg["known_sources"]` relies on.
EMPTY: dict[str, Any] = {
    "solcast": [],
    "open_meteo": [],
    "localvolts_v2": {"entities": [], "stamp_siblings": []},
}


def _loaded(entry: Any) -> bool:
    # ConfigEntryState.LOADED, compared by value so this module needs no
    # config_entries import (keeps the flow's unit-test stubs unchanged).
    return getattr(entry.state, "value", entry.state) == "loaded"


def registry_sensors(
    hass: HomeAssistant, domain: str, matches: Callable[[str], bool] = lambda _u: True
) -> dict[str, str]:
    """unique_id -> entity_id for the enabled sensors of every loaded
    `domain` entry whose built-in unique_id satisfies `matches`. The one
    registry lookup every source-finding path uses."""
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for entry in hass.config_entries.async_entries(domain):
        if not _loaded(entry):
            continue
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
            uid = str(reg.unique_id or "")
            if reg.domain == "sensor" and matches(uid) and reg.disabled_by is None:
                found[uid] = reg.entity_id
    return found


def _single_entry_id(hass: HomeAssistant, domain: str, label: str) -> str | None:
    entries = [e for e in hass.config_entries.async_entries(domain) if _loaded(e)]
    if len(entries) == 1:
        return str(entries[0].entry_id)
    if entries:
        _LOGGER.debug(
            "Nimbus: %d %s entries; not reading one, the site is ambiguous",
            len(entries),
            label,
        )
    return None


def _keyed(found: dict[str, str], uids: tuple[str, ...]) -> list[str]:
    """The entity_ids for `uids`, in that order, skipping any not found."""
    return [found[u] for u in uids if u in found]


def solcast_entities(hass: HomeAssistant) -> list[str]:
    """Solcast's today and tomorrow forecast entities, today first."""
    found = registry_sensors(hass, SOLCAST_DOMAIN, lambda u: u in SOLCAST_KEYS)
    return _keyed(found, SOLCAST_KEYS)


def open_meteo_entities(hass: HomeAssistant) -> list[str]:
    """Open-Meteo Solar Forecast's today .. d7 production entities, today
    first, for a site with exactly one Open-Meteo entry."""
    entry_id = _single_entry_id(hass, OPEN_METEO_DOMAIN, "Open-Meteo Solar Forecast")
    if entry_id is None:
        return []
    uids = tuple(f"{entry_id}_{k}" for k in OPEN_METEO_KEYS)
    found = registry_sensors(hass, OPEN_METEO_DOMAIN, lambda u: u in uids)
    return _keyed(found, uids)


def localvolts_v2(hass: HomeAssistant) -> dict[str, list[str]]:
    """Every LocalVolts v2 sensor (to tell a LocalVolts price apart) and its
    two current-rate sensors (the freshness stamp), for a single account."""
    entry_id = _single_entry_id(hass, LV_V2_DOMAIN, "LocalVolts v2")
    if entry_id is None:
        return {"entities": [], "stamp_siblings": []}
    prefix = f"{entry_id}_"
    found = registry_sensors(hass, LV_V2_DOMAIN, lambda u: u.startswith(prefix))
    return {
        "entities": sorted(found.values()),
        "stamp_siblings": _keyed(found, tuple(prefix + k for k in LV_V2_STAMP_KEYS)),
    }


def resolve(hass: HomeAssistant) -> dict[str, Any]:
    """Every source above, as plain entity_ids, for `cfg["known_sources"]`."""
    return {
        "solcast": solcast_entities(hass),
        "open_meteo": open_meteo_entities(hass),
        "localvolts_v2": localvolts_v2(hass),
    }
