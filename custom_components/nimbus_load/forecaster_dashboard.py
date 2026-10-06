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
* **Once per dashboard, per view.** Each (dashboard, view) handled is
  remembered in Nimbus's own storage, so a household that deletes the tab does
  not get it back on the next restart, while a standard view that a *later*
  release adds still reaches a dashboard an earlier release already visited
  (nimbus #1543: new views must reach existing dashboards, not only new ones).
* **A sections view, never a panel.** A panel view holds one card and nothing
  can be added to it. The added view is a 4-column sections view with one
  full-width section, one card per chart, so a household can add its own
  sections and cards beside them.
* **A title, never an icon.** HA shows a view's icon *instead of* its title, so
  an icon leaves the tab unlabelled. The added view carries no `icon` (household
  instruction, 6 Oct 2026: "use TITLES").
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

STORE_KEY = "nimbus_load.forecaster_view"
STORE_VERSION = 1

# The standard views a release can add to an existing Nimbus dashboard
# (nimbus #1543). Each is identified by the card it holds and remembered per
# dashboard, so a view a later release adds still reaches a dashboard that an
# earlier release already visited. Only views whose card needs no
# household-specific config belong here; the Control Panel needs entity
# mapping and is not auto-added.
STANDARD_VIEWS: tuple[dict[str, Any], ...] = (
    {
        "key": "forecaster",
        "card": "custom:nimbus-forecast-card",
        "title": "Forecaster",
        "alt_title": "Nimbus Forecaster",
        "path": "forecaster",
        "alt_path": "nimbus-forecaster",
        # One card per chart, each full width, so each is a separate card
        # in the section that a household can move, resize or remove.
        "cards": (
            {"chart": "signals"},
            {"chart": "loads"},
        ),
    },
)

# Back-compat names used elsewhere (tests, docs).
CARD_TYPE = STANDARD_VIEWS[0]["card"]
VIEW_TITLE = STANDARD_VIEWS[0]["title"]
ALT_VIEW_TITLE = STANDARD_VIEWS[0]["alt_title"]


def _view_for(spec: dict[str, Any], views: list[Any]) -> dict[str, Any]:
    paths = {str(v.get("path")) for v in views if isinstance(v, dict)}
    titles = {
        str(v.get("title", "")).strip().lower() for v in views if isinstance(v, dict)
    }
    return {
        "title": spec["title"]
        if spec["title"].lower() not in titles
        else spec["alt_title"],
        "path": spec["path"] if spec["path"] not in paths else spec["alt_path"],
        # A sections view, never a panel: a panel view holds exactly one card
        # and nothing else can be added to it (household, 6 Oct 2026: "noone
        # can add anything to these views otherwise"). One full-width section
        # holds the cards; the household can add sections and cards around it.
        "type": "sections",
        "max_columns": 4,
        "sections": [
            {
                "type": "grid",
                "column_span": 4,
                "cards": [
                    {
                        "type": spec["card"],
                        **extra,
                        "grid_options": {"columns": "full", "rows": "auto"},
                    }
                    for extra in spec.get("cards", ({},))
                ],
            }
        ],
    }


def _upgraded_panel_view(view: Any) -> dict[str, Any] | None:
    """The sections layout for a panel view that Nimbus itself added and
    nobody has changed since, or None to leave the view alone.

    Releases before this one added the Forecaster tab as a panel view holding
    exactly one card, `{"type": "custom:nimbus-forecast-card"}`. A panel view
    takes one card and nothing can be added to it, and because each
    dashboard is handled once, deleting the tab would not bring the new one
    back either. So the untouched tab is converted in place, keeping its
    title and path. Anything a household has edited -- another card, a
    wrapper, a different view type -- is not Nimbus's to change.
    """
    if not isinstance(view, dict) or view.get("type") != "panel":
        return None
    for spec in STANDARD_VIEWS:
        if view.get("cards") == [{"type": spec["card"]}] and "sections" not in view:
            upgraded = _view_for(spec, [])
            upgraded["title"] = view.get("title", upgraded["title"])
            if "path" in view:
                upgraded["path"] = view["path"]
            else:
                upgraded.pop("path", None)
            return upgraded
    return None


def _is_nimbus_dashboard(config: dict[str, Any]) -> bool:
    return '"custom:nimbus-' in json.dumps(config)


def _has_card(config: dict[str, Any], card: str) -> bool:
    return f'"{card}"' in json.dumps(config)


async def async_add_forecaster_view(hass: HomeAssistant) -> list[str]:
    """Append each missing standard view to each Nimbus dashboard.

    Returns "<dashboard>:<view key>" for every view added ("default" is the
    default dashboard).
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
        _LOGGER.debug("Nimbus: Lovelace not loaded, standard views skipped")
        return []

    store: Store = Store(hass, STORE_VERSION, STORE_KEY)
    remembered = await store.async_load() or {}
    raw = remembered.get("handled", {})
    handled: dict[str, list[str]] = {
        k: list(v) for k, v in (raw.items() if isinstance(raw, dict) else [])
    }
    before = {k: list(v) for k, v in handled.items()}
    added: list[str] = []

    for url_path, dashboard in list(data.dashboards.items()):
        key = url_path or "default"
        done = handled.setdefault(key, [])
        pending = [v for v in STANDARD_VIEWS if v["key"] not in done]
        if getattr(dashboard, "mode", None) != "storage":
            continue
        try:
            config = await dashboard.async_load(False)
        except ConfigNotFound:
            continue
        if not isinstance(config, dict) or not _is_nimbus_dashboard(config):
            continue
        views = config.get("views")
        if not isinstance(views, list):
            continue
        # Convert a panel tab an earlier release added, if still untouched.
        new_views = []
        upgraded_any = False
        for v in views:
            upgraded = _upgraded_panel_view(v)
            if upgraded is not None:
                upgraded_any = True
                _LOGGER.info(
                    "Nimbus: converted the %r tab on dashboard %s to a sections view",
                    v.get("title"),
                    key,
                )
            new_views.append(upgraded if upgraded is not None else v)
        for spec in pending:
            done.append(spec["key"])
            if _has_card(config, spec["card"]):
                continue
            new_views.append(_view_for(spec, new_views))
            added.append(f"{key}:{spec['key']}")
            _LOGGER.info("Nimbus: added the %s view to dashboard %s", spec["key"], key)
        if upgraded_any or len(new_views) != len(views):
            new_config = dict(config)
            new_config["views"] = new_views
            await dashboard.async_save(new_config)

    handled = {k: v for k, v in handled.items() if v}
    if handled != before:
        await store.async_save({"handled": handled})
    return added
