"""Frontend asset registration -- bundle this project's Lovelace card
resources with the integration itself, so a fresh HACS install gets every
card wired up automatically with no `www/` file copy and no manual
Settings -> Dashboards -> Resources step (issue #79).

Two responsibilities, done at integration setup, once per card:

1. Serve the card's own JS file over HTTP at a stable, integration-owned
   URL (`/nimbus_load/<filename>`), so it's reachable by the browser
   without living under the user's `www/` folder.
2. Register that URL as an "extra JS module" via
   homeassistant.components.frontend.add_extra_js_url() -- HA's own
   documented mechanism for an integration to inject frontend JS. The
   HA frontend loads every URL in this set at startup, so any dashboard
   (storage-mode or YAML-mode) resolves `type: custom:<card-type>` the
   moment the user drops it in.
   Deliberately NOT the `hass.data["lovelace"].resources` collection --
   that path mutates user-managed Lovelace storage, depends on Lovelace-
   internal data shape, and skips YAML-mode installs; add_extra_js_url
   avoids all three.

Both steps are idempotent -- running through this again on a reload (or
on every subsequent HA startup) does nothing observable: each static
path is guarded by its own flag, and add_extra_js_url stores URLs in a
frozenset. A new nimbus release bumps every URL's `?v=` cache-buster, so
browsers pick up the newer file on the next dashboard load.

Kept deliberately narrow: this module owns ONLY the frontend-asset side
of the integration. Nothing about coordinators, forecasting, or the
solver runs through here -- those all continue to live where they
already do (coordinator.py, ml/, solver/, solver_runtime.py).

2026-09-06 (nimbus issue #364, Mark Purcell): generalized from a single
hardcoded topology-card registration into a small table of (filename,
card type) pairs, to also ship nimbus-dispatch-card-v4.js ("Control
Panel") and nimbus-regret-card.js ("Regret") -- the same real dashboard
views a household already runs, now installable by anyone via HACS
instead of a hand-copied `www/` file. The dispatch card's own household-
specific entity_ids (battery/grid/solar/mode/armed sensors, previously
hardcoded) are resolved from the card's own YAML `config:` block instead
-- see that file's own setConfig() and docs/dashboards.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend"


class _CardAsset(NamedTuple):
    filename: str
    card_type: str


# Every Lovelace card resource this integration ships. Adding a new card
# here is the entire "ship a new dashboard view with nimbus" step -- no
# other wiring needed (see async_register_frontend() below).
_CARDS: tuple[_CardAsset, ...] = (
    # nimbus issue #519: renamed from switchboard-topology-card.js /
    # "switchboard-topology-card" -- the old type had neither "nimbus" in
    # its name nor its picker name, so a user searching the card picker
    # for "nimbus" (having already found the other two cards that way)
    # concluded this one wasn't deployed. The old custom-element tag is
    # kept registered as an alias inside the JS file itself, so an
    # existing dashboard's `type: custom:switchboard-topology-card` keeps
    # rendering -- only the served filename and the card-picker identity
    # change here.
    _CardAsset("nimbus-topology-card.js", "nimbus-topology-card"),
    _CardAsset("nimbus-dispatch-card-v4.js", "nimbus-dispatch-card-v4"),
    _CardAsset("nimbus-regret-card.js", "nimbus-regret-card"),
)


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve every shipped card's JS and register each as an extra JS module.

    Called once per HA lifetime from async_setup_entry -- guarded per-card
    so a reload or a second config entry (there is only ever one hub, but
    the guard is cheap and correct) doesn't re-register the same static
    path. add_extra_js_url is safely idempotent on its own via frozenset
    dedup; called unconditionally so a version-bump refreshes the cache-
    buster on every card.
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

    for card in _CARDS:
        url_path = f"/{DOMAIN}/{card.filename}"
        registered_flag = f"{DOMAIN}_frontend_registered_{card.filename}"

        # 1) Serve the static file. Guarded by a hass.data flag so a
        # reload doesn't try to re-register the same path (HA raises on
        # that).
        if not hass.data.get(registered_flag):
            card_path = _FRONTEND_DIR / card.filename
            if not card_path.is_file():
                _LOGGER.error(
                    "Nimbus: %s asset missing at %s -- the integration was "
                    "installed incompletely (HACS didn't copy the frontend/ "
                    "folder), skipping frontend registration for this card",
                    card.card_type,
                    card_path,
                )
                continue
            await hass.http.async_register_static_paths(
                [
                    StaticPathConfig(
                        url_path=url_path,
                        path=str(card_path),
                        cache_headers=False,
                    )
                ]
            )
            hass.data[registered_flag] = True
            _LOGGER.debug("Nimbus: serving %s from %s", url_path, card_path)

        # 2) Tell HA's frontend to load this URL as a JS module at
        # startup. Public, documented API (frontend.add_extra_js_url); a
        # synchronous call that mutates a frozenset in hass.data -- no
        # storage writes, no dependency on Lovelace's own data shape, and
        # works identically for storage-mode and YAML-mode Lovelace.
        versioned_url = f"{url_path}?v={version}"
        add_extra_js_url(hass, versioned_url)
        _LOGGER.info(
            "Nimbus: %s registered at %s -- available in every dashboard "
            "as `type: custom:%s`",
            card.card_type,
            versioned_url,
            card.card_type,
        )
