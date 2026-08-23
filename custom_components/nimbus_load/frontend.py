"""Frontend asset registration -- bundle the switchboard-topology-card
Lovelace resource with the integration itself, so a fresh HACS install
gets the card wired up automatically with no `www/` file copy and no
manual Settings -> Dashboards -> Resources step (issue #79).

Two responsibilities, done at integration setup:

1. Serve the card's own JS file over HTTP at a stable, integration-owned
   URL (`/nimbus_load/switchboard-topology-card.js`), so it's reachable
   by the browser without living under the user's `www/` folder.
2. Register that URL as an "extra JS module" via
   homeassistant.components.frontend.add_extra_js_url() -- HA's own
   documented mechanism for an integration to inject frontend JS. The
   HA frontend loads every URL in this set at startup, so any dashboard
   (storage-mode or YAML-mode) resolves `type:
   custom:switchboard-topology-card` the moment the user drops it in.
   Deliberately NOT the `hass.data["lovelace"].resources` collection --
   that path mutates user-managed Lovelace storage, depends on Lovelace-
   internal data shape, and skips YAML-mode installs; add_extra_js_url
   avoids all three.

Both steps are idempotent -- running through this again on a reload (or
on every subsequent HA startup) does nothing observable: the static
path is guarded by a flag, and add_extra_js_url stores URLs in a
frozenset. A new nimbus release bumps the URL's `?v=` cache-buster, so
browsers pick up the newer file on the next dashboard load.

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
# card).
FRONTEND_URL_PATH = f"/{DOMAIN}/switchboard-topology-card.js"

# The card's registered type name (window.customCards / customElements
# name). Fixed and stable -- an existing dashboard's card config uses
# `type: custom:switchboard-topology-card`, renaming this would silently
# break every dashboard already using the card.
CARD_TYPE = "switchboard-topology-card"

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_CARD_FILENAME = "switchboard-topology-card.js"


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve the topology card's JS and register it as an extra JS module.

    Called once per HA lifetime from async_setup_entry -- guarded so a
    reload or a second config entry (there is only ever one hub, but the
    guard is cheap and correct) doesn't re-register the same static path.
    add_extra_js_url is safely idempotent on its own via frozenset
    dedup; we call it unconditionally so a version-bump refreshes the
    cache-buster.
    """
    # Deferred imports -- same reasoning as sensor.py's own deferred
    # `from . import solver_writer` (see that file's own comment) and
    # this module's own earlier StaticPathConfig fix (55e250c0): a
    # module-level import here drags homeassistant.components.http /
    # homeassistant.components.frontend into every test that imports
    # anything from custom_components.nimbus_load (nearly all of them
    # via __init__.py), and tests/_ha_stubs.py has no stub for either
    # -- confirmed live before that fix, this broke the whole local
    # suite at collection time.
    from homeassistant.components.frontend import add_extra_js_url
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
        _LOGGER.debug("Nimbus: serving %s from %s", FRONTEND_URL_PATH, card_path)

    # 2) Tell HA's frontend to load this URL as a JS module at startup.
    # Public, documented API (frontend.add_extra_js_url); a synchronous
    # call that mutates a frozenset in hass.data -- no storage writes,
    # no dependency on Lovelace's own data shape, and works identically
    # for storage-mode and YAML-mode Lovelace.
    versioned_url = f"{FRONTEND_URL_PATH}?v={version}"
    add_extra_js_url(hass, versioned_url)
    _LOGGER.info(
        "Nimbus: switchboard-topology-card registered at %s -- available "
        "in every dashboard as `type: custom:switchboard-topology-card`",
        versioned_url,
    )
