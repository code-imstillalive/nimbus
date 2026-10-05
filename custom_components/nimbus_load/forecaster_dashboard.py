"""The Forecaster tab, added to the household's Nimbus dashboard automatically
(nimbus #1529).

Registering a card is not the same as it reaching a screen (#550/#552). The
Forecaster charts are the clearest case: a tester updated, restarted and
found nothing new to look at. So on startup Nimbus adds a **Forecaster** view
(a tab) to the dashboard the household already uses for Nimbus, next to its
Control Panel, Topology and Regret views. The view holds one card,
`custom:nimbus-forecast-card`, which discovers every Load, Power Signal,
weather signal and the Solver's own series by itself, so the view never needs
updating.

Which dashboard
---------------
Any storage-mode dashboard that already holds one of Nimbus's own cards
(`custom:nimbus-*`). That identifies "the Nimbus dashboard" on every install,
whatever it was named, without a setting.

Rules it keeps
--------------
* **Adds a view, changes nothing else.** The whole config is loaded, one view
  is appended, and the same config is saved, so every existing view, card and
  `card_mod` style is carried through untouched.
* **Never duplicates the card.** A dashboard that already shows
  `custom:nimbus-forecast-card` anywhere is left alone. A household's own
  view that happens to be titled "Forecaster" (its own charts, not this card)
  is never touched either: the new tab is then titled "Nimbus Forecaster" and
  sits beside it (household decision, 5 Oct 2026).
* **Once per dashboard.** Each dashboard handled is remembered in Nimbus's own
  storage, so a household that deletes the tab does not get it back on the
  next restart.
* **YAML-mode dashboards are skipped:** they cannot be written to.
* **Non-fatal.** Lovelace absent, recovery mode or an internal API change logs
  and leaves the rest of Nimbus running.

`LovelaceStorage.async_load` / `async_save` and `hass.data[LOVELACE_DATA]
.dashboards` were read against HA 2026.7.4 and 2026.9.3, where they are
identical.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CARD_TYPE = "custom:nimbus-forecast-card"
VIEW_TITLE = "Forecaster"
ALT_VIEW_TITLE = "Nimbus Forecaster"
STORE_KEY = "nimbus_load.forecaster_view"
STORE_VERSION = 1


def _forecaster_view(views: list[Any]) -> dict[str, Any]:
    paths = {str(v.get("path")) for v in views if isinstance(v, dict)}
    titles = {
        str(v.get("title", "")).strip().lower() for v in views if isinstance(v, dict)
    }
    path = "forecaster" if "forecaster" not in paths else "nimbus-forecaster"
    title = VIEW_TITLE if VIEW_TITLE.lower() not in titles else ALT_VIEW_TITLE
    return {
        "title": title,
        "path": path,
        "icon": "mdi:chart-timeline-variant",
        "type": "panel",
        "cards": [{"type": CARD_TYPE}],
    }


def _is_nimbus_dashboard(config: dict[str, Any]) -> bool:
    text = json.dumps(config)
    return '"custom:nimbus-' in text


def _already_has_card(config: dict[str, Any]) -> bool:
    return f'"{CARD_TYPE}"' in json.dumps(config)


async def async_add_forecaster_view(hass: HomeAssistant) -> list[str]:
    """Add the Forecaster tab to each Nimbus dashboard that lacks one.

    Returns the dashboards it was added to ("default" for the default one).
    """
    # Deferred imports, same reasoning as frontend.py: a module-level import
    # drags homeassistant.components.* into every unit test.
    from homeassistant.components.lovelace.const import (
        LOVELACE_DATA,
        ConfigNotFound,
    )
    from homeassistant.helpers.storage import Store

    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        _LOGGER.debug("Nimbus: Lovelace not loaded, Forecaster tab skipped")
        return []

    store: Store = Store(hass, STORE_VERSION, STORE_KEY)
    remembered = await store.async_load() or {}
    handled: list[str] = list(remembered.get("handled", []))
    added: list[str] = []

    for url_path, dashboard in list(data.dashboards.items()):
        key = url_path or "default"
        if key in handled:
            continue
        if getattr(dashboard, "mode", None) != "storage":
            continue
        try:
            config = await dashboard.async_load(False)
        except ConfigNotFound:
            continue
        if not isinstance(config, dict) or not _is_nimbus_dashboard(config):
            continue
        handled.append(key)
        if _already_has_card(config):
            continue
        views = config.get("views")
        if not isinstance(views, list):
            continue
        new_config = dict(config)
        new_config["views"] = [*views, _forecaster_view(views)]
        await dashboard.async_save(new_config)
        added.append(key)
        _LOGGER.info("Nimbus: added the Forecaster tab to dashboard %s", key)

    if handled != remembered.get("handled", []):
        await store.async_save({"handled": handled})
    return added
