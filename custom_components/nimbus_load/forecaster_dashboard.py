"""The Forecaster view, added to every install automatically (nimbus #1529).

Registering a card is not the same as it reaching a screen (#550/#552): a
household that never hand-authors dashboard YAML has no way to discover a
card exists. The Forecaster charts are the clearest case -- a tester
updated, restarted and found nothing new to look at. So Nimbus puts the
view in front of the user itself: a "Nimbus Forecaster" sidebar dashboard
whose one view is `custom:nimbus-forecast-card`, which discovers every
Load, Power Signal, weather signal and the Solver's own series by itself.

How, and why this way
---------------------
Home Assistant creates its own Map dashboard on first start the same way in
spirit (`lovelace._create_map_dashboard`), but through the dashboards
collection, which is a local variable inside Lovelace's setup and not
reachable from an integration. Opening a second collection over the same
store would race the real one and lose writes. Instead this does what
Lovelace does internally for each storage dashboard: a `LovelaceStorage`
in `hass.data[LOVELACE_DATA].dashboards` plus a built-in "lovelace" panel.
Read against HA 2026.7.4 and 2026.9.3, where `LovelaceStorage`,
`_register_panel`'s arguments and `async_register_built_in_panel` are
identical.

Rules it keeps:

* **Never touches a household dashboard.** It only ever creates its own
  url path, and does nothing at all if that path or panel already exists.
* **Seeds once.** The view is written only when this dashboard has no saved
  config. A household's own edits to it (storage mode, editable in the UI)
  are never overwritten.
* **The card needs no updating.** Everything it shows is discovered at load
  time, so a new Load or a new release appears without any config change.
* **Non-fatal.** Lovelace in YAML mode, recovery mode or an internal API
  change logs once and leaves the rest of Nimbus running.

The dashboard is registered on each start rather than persisted in the
dashboards list, so it does not appear under Settings -> Dashboards. It can
be hidden per user from the sidebar like any other panel.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

URL_PATH = "nimbus-forecaster"
STORAGE_ID = "nimbus_forecaster"
TITLE = "Nimbus Forecaster"
ICON = "mdi:chart-timeline-variant"

DEFAULT_CONFIG: dict[str, Any] = {
    "title": TITLE,
    "views": [
        {
            "title": "Forecaster",
            "path": "forecaster",
            "icon": ICON,
            "type": "panel",
            "cards": [{"type": "custom:nimbus-forecast-card"}],
        }
    ],
}

_DONE_FLAG = "nimbus_load_forecaster_dashboard_registered"


async def async_register_forecaster_dashboard(hass: HomeAssistant) -> bool:
    """Register the Nimbus Forecaster sidebar dashboard. Returns True when
    the panel is registered by this call, False when skipped for any reason.
    """
    if hass.data.get(_DONE_FLAG):
        return False
    # Deferred imports, same reasoning as frontend.py: a module-level import
    # of these drags homeassistant.components.* into every unit test.
    from homeassistant.components.frontend import (
        async_panel_exists,
        async_register_built_in_panel,
    )
    from homeassistant.components.lovelace.const import (
        LOVELACE_DATA,
        ConfigNotFound,
    )
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        _LOGGER.debug("Nimbus: Lovelace not loaded, Forecaster dashboard skipped")
        return False
    if URL_PATH in data.dashboards or async_panel_exists(hass, URL_PATH):
        _LOGGER.debug("Nimbus: %s already exists, left untouched", URL_PATH)
        hass.data[_DONE_FLAG] = True
        return False

    store = LovelaceStorage(
        hass,
        {
            "id": STORAGE_ID,
            "url_path": URL_PATH,
            "title": TITLE,
            "icon": ICON,
            "mode": "storage",
            "require_admin": False,
            "show_in_sidebar": True,
        },
    )
    try:
        await store.async_load(False)
    except ConfigNotFound:
        await store.async_save(DEFAULT_CONFIG)
        _LOGGER.info("Nimbus: created the %s dashboard", TITLE)

    data.dashboards[URL_PATH] = store
    async_register_built_in_panel(
        hass,
        "lovelace",
        sidebar_title=TITLE,
        sidebar_icon=ICON,
        frontend_url_path=URL_PATH,
        config={"mode": "storage"},
        require_admin=False,
    )
    hass.data[_DONE_FLAG] = True
    return True
