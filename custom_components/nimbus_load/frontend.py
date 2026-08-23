"""Frontend asset registration -- bundle the switchboard-topology-card
Lovelace resource with the integration itself, so a fresh HACS install
gets the card wired up automatically with no `www/` file copy and no
manual Settings -> Dashboards -> Resources step (issue #79).

Two responsibilities, done at integration setup:

1. Serve the card's own JS file over HTTP at a stable, integration-owned
   URL (`/nimbus_load/switchboard-topology-card.js`), so it's reachable
   by the browser without living under the user's `www/` folder.
2. Auto-register that URL as a Lovelace resource of type `module`, so a
   dashboard's `switchboard-topology-card` card type resolves the moment
   the user drops one into a view -- same result as manually pasting the
   URL into Settings -> Dashboards -> Resources, without the user ever
   having to.

Both are idempotent -- running through this again on a reload (or on
every subsequent HA startup) does nothing if the static path is already
registered and the resource already exists, and it patches the resource's
URL forward to the current integration version if `?v=` drifted (cheap
cache-bust for a new nimbus release).

Kept deliberately narrow: this module owns ONLY the frontend-asset side
of the integration. Nothing about coordinators, forecasting, or the
solver runs through here -- those all continue to live where they
already do (coordinator.py, ml/, solver/, solver_runtime.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Public URL the browser fetches the card from. Namespaced under the
# integration's own domain so it can't collide with anything the user
# has in `www/` (or with any other custom integration that ships a
# card). Same convention already used by other frontend-bundling
# integrations (e.g. `browser_mod`, `mini-graph-card`-via-integration).
FRONTEND_URL_PATH = f"/{DOMAIN}/switchboard-topology-card.js"

# The card's registered type name (window.customCards / customElements
# name). Fixed and stable -- an existing dashboard's card config uses
# `type: custom:switchboard-topology-card`, renaming this would silently
# break every dashboard already using the card.
CARD_TYPE = "switchboard-topology-card"

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_CARD_FILENAME = "switchboard-topology-card.js"


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the topology card's JS and register the Lovelace resource.

    Called once per HA lifetime from async_setup_entry -- guarded so a
    reload or a second config entry (there is only ever one hub, but the
    guard is cheap and correct) doesn't re-register the same static path
    or duplicate the resource.
    """
    # Deferred import -- same reasoning as sensor.py's own deferred
    # `from . import solver_writer` (see that file's own comment):
    # a module-level import here dragged StaticPathConfig into every
    # single test that imports anything from custom_components.
    # nimbus_load (i.e. nearly all of them, via __init__.py), and
    # tests/_ha_stubs.py has no stub for homeassistant.components.
    # http -- confirmed live, this broke the whole local suite
    # (tests/run_all.py) at collection time before this fix.
    from homeassistant.components.http import StaticPathConfig

    # 1) Serve the static file. Guarded by a hass.data flag so a reload
    # doesn't try to re-register the same path (HA raises on that).
    if not hass.data.get(f"{DOMAIN}_frontend_registered"):
        card_path = _FRONTEND_DIR / _CARD_FILENAME
        if not card_path.is_file():
            _LOGGER.error(
                "Nimbus: topology card asset missing at %s -- the integration "
                "was installed incompletely (HACS didn't copy the frontend/ "
                "folder), skipping frontend registration",
                card_path,
            )
            return
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    url_path=FRONTEND_URL_PATH,
                    path=str(card_path),
                    cache_headers=False,
                )
            ]
        )
        hass.data[f"{DOMAIN}_frontend_registered"] = True
        _LOGGER.debug(
            "Nimbus: serving %s from %s", FRONTEND_URL_PATH, card_path
        )

    # 2) Auto-register the Lovelace resource. Nothing to do in YAML-mode
    # Lovelace (the user manages resources themselves in configuration.
    # yaml under `lovelace: resources:`) -- only storage-mode has a
    # writeable resources collection at hass.data["lovelace"].resources.
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        _LOGGER.debug(
            "Nimbus: Lovelace integration not loaded yet, skipping "
            "resource auto-registration"
        )
        return
    resources = getattr(lovelace_data, "resources", None)
    if resources is None:
        _LOGGER.debug(
            "Nimbus: Lovelace resources collection unavailable "
            "(YAML mode?), user will need to add %s manually",
            FRONTEND_URL_PATH,
        )
        return

    # ResourceStorageCollection lazily loads on the first mutating call
    # (async_create_item/async_update_item both call _async_ensure_loaded
    # internally as of HA 2024.4). Read-only enumeration via
    # async_items() sees an empty list until then, which would look like
    # "no existing resource" and trigger a duplicate create on a warm
    # restart -- so force the load ourselves before checking.
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    versioned_url = f"{FRONTEND_URL_PATH}?v={version}"

    # Look for an existing Nimbus resource. Match on the un-versioned
    # URL prefix so a version bump patches the existing entry forward
    # rather than creating a second copy every release.
    existing = None
    for item in resources.async_items():
        url = item.get("url", "")
        if url.split("?", 1)[0] == FRONTEND_URL_PATH:
            existing = item
            break

    if existing is None:
        await resources.async_create_item(
            {"res_type": "module", "url": versioned_url}
        )
        _LOGGER.info(
            "Nimbus: registered Lovelace resource %s -- the "
            "switchboard-topology-card is now available in every dashboard",
            versioned_url,
        )
        return

    if existing.get("url") != versioned_url:
        await resources.async_update_item(
            existing["id"], {"res_type": "module", "url": versioned_url}
        )
        _LOGGER.debug(
            "Nimbus: bumped topology-card resource to %s", versioned_url
        )
