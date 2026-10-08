"""The standard tabs of the Nimbus dashboard (nimbus #1529, #1543).

Registering a card is not the same as it reaching a screen (#550/#552): a
tester updated, restarted and found nothing new to look at. So Nimbus keeps
the standard tabs on the install's Nimbus dashboard, in this order:

    Forecaster, Solver, Topology, Control Panel, Regret

Every card discovers its own entities -- the Control Panel's blank fields
follow the Solver config, and the Solver tab (nimbus #1594, `solver_tab.py`)
is resolved from Nimbus's own entity registry -- so nobody configures
anything. An existing dashboard that already has a household-built tab titled
**Solver** gets Nimbus's Solver tab **beside** it, under the same title, for
comparison (rule 4; household, 8 Oct 2026: "install itself at the end or
next to it"); the household's own tab is never touched.

The rules, agreed by the household and Mark Purcell on 6 Oct 2026
------------------------------------------------------------------
1. **An install with no Nimbus dashboard** gets one, "Nimbus", holding the
   five tabs. It is created once: delete it and it stays deleted.
2. **A missing tab is added**, at the end of the tab row.
3. **A tab Nimbus added and nobody has changed is updated in place** when a
   release changes its design ("Happy for it to replace if it hasn't been
   modified" -- Mark).
4. **A tab the household changed, or built themselves, is never touched.**
   When a release changes that tab's design, the new version is added
   *beside* it, under the same plain title, for comparison ("if modified add
   new one next to it for comparison by the user" -- household).
5. **Only on a real design change.** A tab that already shows its card is
   left alone until Nimbus's own design of that tab changes, so a deploy or a
   restart on its own adds nothing.
6. **A tab the household deleted comes back once**, and only when a release
   changes its design.
7. **Never deletes, edits or reorders** any of the household's tabs or cards.
8. **A title on every tab, never an icon** -- HA shows a view's icon instead
   of its title -- and never "Nimbus" in a tab title: the tabs already sit on
   the Nimbus dashboard (household, 6 Oct 2026).

How "untouched" is told from "changed"
--------------------------------------
For each tab it writes, Nimbus remembers a fingerprint of exactly what it
wrote and a fingerprint of the design it wrote (`views` in its own storage,
per dashboard and per tab). A tab whose content still matches what Nimbus
wrote is untouched. A tab Nimbus never wrote (the household's own) is never
"untouched".

Which dashboard
---------------
**Contained** (household, 6 Oct 2026: "no nimbus stuff should ever sit
outside of nimbus dashboard eg home... it must be contained"). Nimbus writes
to one dashboard only: a storage dashboard whose url path or title says
Nimbus (the one with the most Nimbus cards, if several). Every other
dashboard -- Home included, whatever Nimbus cards a household put on it -- is
never written. With no Nimbus dashboard, Nimbus creates its own. The topology card's pre-#519 name, `switchboard-topology-card`,
counts as the topology card everywhere, so a dashboard carrying it is never
given a duplicate Topology tab. YAML dashboards are never written.

Creating the dashboard
----------------------
Home Assistant has no public API for an integration to create a dashboard:
the storage dashboards collection is private to the lovelace component. Its
own `lovelace/dashboards/create` websocket command is registered in
`hass.data["websocket_api"]`, wrapped in `functools.wraps` decorators, and
unwraps to the collection's bound `ws_create_item`, whose `__self__` holds
the collection. Creating through that same collection keeps the UI's list in
step (its listener registers the panel), which a second collection instance
would not -- that one would be overwritten the next time the household edits
a dashboard. Read against HA 2026.7.4 and 2026.9.3, where it is identical. If
it is ever not found, nothing is created and a log line says so.

Non-fatal throughout: Lovelace absent, recovery mode or an internal API
change logs and leaves the rest of Nimbus running.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

try:
    from . import solver_tab
except ImportError:  # pragma: no cover - loaded as a top-level module in tests
    import solver_tab  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "nimbus_load.forecaster_view"
STORE_VERSION = 1

DASHBOARD_URL_PATH = "dashboard-nimbus"
DASHBOARD_TITLE = "Nimbus"

# The standard tabs, in order. `match` lists every card type that counts as
# this tab's card being on the dashboard already.
STANDARD_VIEWS: tuple[dict[str, Any], ...] = (
    {
        "key": "forecaster",
        "card": "custom:nimbus-forecast-card",
        "title": "Forecaster",
        "path": "forecaster",
        # One card per chart, each full width, so each is a separate card
        # in the section that a household can move, resize or remove.
        "cards": (
            {"chart": "signals"},
            {"chart": "loads"},
        ),
    },
    {
        "key": "solver",
        "title": "Solver",
        "path": "solver",
        # Standard HA cards built from solver_tab.template(), not one Nimbus
        # card: `match` is the one entity the tab always shows.
        "sections_template": True,
        "match": ("sensor.nimbus_solver_lp_status",),
    },
    {
        "key": "topology",
        "card": "custom:nimbus-topology-card",
        "match": ("custom:nimbus-topology-card", "custom:switchboard-topology-card"),
        "title": "Topology",
        "path": "topology",
    },
    {
        "key": "control_panel",
        "card": "custom:nimbus-dispatch-card-v4",
        "title": "Control Panel",
        "path": "control-panel",
    },
    {
        "key": "regret",
        "card": "custom:nimbus-regret-card",
        "title": "Regret",
        "path": "regret",
    },
)

_NIMBUS_CARD_MARKERS = ('"custom:nimbus-', '"custom:switchboard-topology-card"')

# Back-compat names used elsewhere (tests, docs).
CARD_TYPE = STANDARD_VIEWS[0]["card"]
VIEW_TITLE = STANDARD_VIEWS[0]["title"]


def _view_for(spec: dict[str, Any], path: str | None = None) -> dict[str, Any]:
    if spec.get("sections_template"):
        return {
            "title": spec["title"],
            "path": path or spec["path"],
            "type": "sections",
            "max_columns": 4,
            "sections": solver_tab.template(),
        }
    return {
        "title": spec["title"],
        "path": path or spec["path"],
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


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def design_of(spec: dict[str, Any]) -> str:
    """A release's design of one standard tab; changes when the tab does."""
    return _fingerprint(_view_for(spec))


def _upgraded_panel_view(view: Any) -> dict[str, Any] | None:
    """The sections layout for a panel view that Nimbus itself added and
    nobody has changed since, or None to leave the view alone.

    v0.94.438/439 added the Forecaster tab as a panel view holding exactly
    one card, `{"type": "custom:nimbus-forecast-card"}`. A panel view takes
    one card and nothing can be added to it, so the untouched tab is
    converted in place, keeping its title and path.
    """
    if not isinstance(view, dict) or view.get("type") != "panel":
        return None
    for spec in STANDARD_VIEWS:
        if (
            "card" in spec
            and view.get("cards") == [{"type": spec["card"]}]
            and "sections" not in view
        ):
            upgraded = _view_for(spec)
            upgraded["title"] = view.get("title", upgraded["title"])
            if "path" in view:
                upgraded["path"] = view["path"]
            else:
                upgraded.pop("path", None)
            return upgraded
    return None


def _nimbus_card_count(config: Any) -> int:
    text = json.dumps(config)
    return sum(text.count(marker) for marker in _NIMBUS_CARD_MARKERS)


def _holds_card(view: Any, spec: dict[str, Any]) -> bool:
    text = json.dumps(view)
    return any(f'"{card}"' in text for card in spec.get("match") or (spec["card"],))


def _free_path(spec: dict[str, Any], views: list[Any]) -> str:
    taken = {v.get("path") for v in views if isinstance(v, dict)}
    path, n = spec["path"], 2
    while path in taken:
        path, n = f"{spec['path']}-{n}", n + 1
    return path


def _pick_nimbus_dashboard(
    loaded: list[tuple[Any, Any, dict[str, Any]]],
) -> tuple[Any, Any, dict[str, Any]] | None:
    """The install's Nimbus dashboard: a storage dashboard whose url path or
    title says Nimbus, the one with the most Nimbus cards if there are
    several. Never any other dashboard, whatever Nimbus cards it holds
    (household, 6 Oct 2026: "no nimbus stuff should ever sit outside of
    nimbus dashboard eg home... it must be contained")."""
    candidates = [
        (url, dash, config)
        for url, dash, config in loaded
        if getattr(dash, "mode", None) == "storage"
        and isinstance(config.get("views"), list)
        and "nimbus" in f"{url or ''} {config.get('title', '')}".lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: _nimbus_card_count(item[2]))


def _sync_views(
    views: list[Any],
    mem: dict[str, Any],
    legacy: list[str],
    *,
    created: bool = False,
    resolve: Any = None,
) -> tuple[list[Any], list[str]]:
    """Apply the rules in the module doc to one dashboard's views. Returns the
    new views and what was done; updates `mem` in place."""
    views = [_upgraded_panel_view(v) or v for v in views]
    done: list[str] = []

    def build(spec: dict[str, Any], path: str) -> dict[str, Any]:
        # A templated tab (the Solver tab) is written with this install's
        # entity_ids; its DESIGN stays the template, the same everywhere.
        view = _view_for(spec, path)
        if resolve is not None and spec.get("sections_template"):
            view = resolve(view)
        return view

    for spec in STANDARD_VIEWS:
        if spec.get("sections_template") and resolve is None:
            # Nothing to resolve the template's entities against: never
            # write `@key` placeholders onto a dashboard.
            continue
        key, design = spec["key"], design_of(spec)
        rec = mem.get(key)
        if rec is None and key in legacy:
            # An earlier release (v0.94.438-441) already handled this tab
            # here: what is on the dashboard now is the household's.
            mem[key] = {"design": design, "path": None, "written": None}
            continue
        if rec is None:
            if any(_holds_card(v, spec) for v in views):
                # Rule 5: the card is there already; nothing until the design
                # changes.
                mem[key] = {"design": design, "path": None, "written": None}
                continue
            view = build(spec, _free_path(spec, views))
            # A household tab with this title (their own Solver tab) gets the
            # new one beside it, for comparison (rule 4); otherwise the end.
            same_title = [
                i
                for i, v in enumerate(views)
                if isinstance(v, dict) and v.get("title") == spec["title"]
            ]
            if same_title:
                views.insert(same_title[-1] + 1, view)
            else:
                views.append(view)
            mem[key] = {
                "design": design,
                "path": view["path"],
                "written": _fingerprint(view),
            }
            done.append(f"added:{key}")
            continue
        if rec.get("design") == design:
            continue
        idx = next(
            (
                i
                for i, v in enumerate(views)
                if rec.get("path")
                and isinstance(v, dict)
                and v.get("path") == rec["path"]
            ),
            None,
        )
        if idx is not None and rec.get("written") == _fingerprint(views[idx]):
            # Rule 3: untouched since Nimbus wrote it -- update in place.
            view = build(spec, views[idx]["path"])
            views[idx] = view
            done.append(f"updated:{key}")
        else:
            # Rules 4 and 6: changed, the household's own, or deleted -- the
            # new design goes beside it (or at the end), once.
            if idx is None:
                holding = [i for i, v in enumerate(views) if _holds_card(v, spec)]
                idx = holding[-1] if holding else None
            view = build(spec, _free_path(spec, views))
            views.insert(len(views) if idx is None else idx + 1, view)
            done.append(f"beside:{key}")
        mem[key] = {
            "design": design,
            "path": view["path"],
            "written": _fingerprint(view),
        }
    return views, done


def _dashboards_collection(hass: HomeAssistant) -> Any:
    """The lovelace storage dashboards collection, or None (see module doc)."""
    handlers = hass.data.get("websocket_api")
    if not isinstance(handlers, dict):
        return None
    registered = handlers.get("lovelace/dashboards/create")
    handler = registered[0] if isinstance(registered, tuple) else registered
    if handler is None:
        return None
    owner = getattr(inspect.unwrap(handler), "__self__", None)
    collection = getattr(owner, "storage_collection", None)
    if collection is None or not hasattr(collection, "async_create_item"):
        return None
    return collection


async def _async_load(dashboard: Any) -> Any:
    """A dashboard's config, or None when it has none or cannot be read (an
    empty storage dashboard, a broken YAML file)."""
    try:
        return await dashboard.async_load(False)
    except Exception:  # noqa: BLE001 - ConfigNotFound, a YAML error: unreadable
        return None


async def _async_create_dashboard(hass: HomeAssistant, data: Any) -> Any:
    collection = _dashboards_collection(hass)
    if collection is None:
        _LOGGER.warning(
            "Nimbus: could not find Home Assistant's dashboards collection, so "
            "the Nimbus dashboard was not created. Build it by hand from "
            "docs/dashboards.md (Forecaster, Solver, Topology, Control Panel, Regret)"
        )
        return None
    await collection.async_create_item(
        {
            "title": DASHBOARD_TITLE,
            "url_path": DASHBOARD_URL_PATH,
            "require_admin": False,
            "show_in_sidebar": True,
        }
    )
    dashboard = data.dashboards.get(DASHBOARD_URL_PATH)
    if dashboard is None:
        _LOGGER.warning(
            "Nimbus: created the Nimbus dashboard but Home Assistant did not "
            "register it; build its tabs by hand from docs/dashboards.md"
        )
    return dashboard


def _solver_resolver(hass: HomeAssistant) -> Any:
    """`solver_tab.resolve` bound to this install: `@<key>` is the entity
    whose unique id is `<nimbus entry id>_<key>`, `@option:<name>` the
    hub's option. None when there is no Nimbus entry to resolve against."""
    from homeassistant.helpers import entity_registry as er

    config_entries = getattr(hass, "config_entries", None)
    entries = config_entries.async_entries("nimbus_load") if config_entries else []
    if not entries:
        return None
    entry = entries[0]
    by_uid = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }

    def lookup(key: str) -> str | None:
        if key.startswith("option:"):
            value = entry.options.get(key.split(":", 1)[1])
            return value if isinstance(value, str) and value else None
        return by_uid.get(f"{entry.entry_id}_{key}")

    return lambda view: solver_tab.resolve(view, lookup)


async def async_ensure_nimbus_dashboard(hass: HomeAssistant) -> list[str]:
    """Keep the standard tabs on the install's Nimbus dashboard (module doc).
    Returns what it did, for logs and tests."""
    # Deferred imports, same reasoning as frontend.py: a module-level import
    # drags homeassistant.components.* into every unit test.
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.helpers.storage import Store

    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        _LOGGER.debug("Nimbus: Lovelace not loaded, Nimbus dashboard skipped")
        return []

    loaded = []
    for url_path, dashboard in list(data.dashboards.items()):
        config = await _async_load(dashboard)
        if isinstance(config, dict):
            loaded.append((url_path, dashboard, config))

    store: Store = Store(hass, STORE_VERSION, STORE_KEY)
    remembered = await store.async_load() or {}
    if not isinstance(remembered, dict):
        remembered = {}
    before = json.dumps(remembered, sort_keys=True)
    legacy = remembered.get("handled")
    legacy = legacy if isinstance(legacy, dict) else {}
    memory = remembered.setdefault("views", {})

    done: list[str] = []
    target = _pick_nimbus_dashboard(loaded)
    if target is None:
        if remembered.get("created") or DASHBOARD_URL_PATH in data.dashboards:
            # Created once, ever (the household deleted it); and never a path
            # something else owns, such as a YAML dashboard.
            return done
        dashboard = await _async_create_dashboard(hass, data)
        if dashboard is None:
            return done
        remembered["created"] = DASHBOARD_URL_PATH
        target = (DASHBOARD_URL_PATH, dashboard, {"views": []})
        done.append(f"created:{DASHBOARD_URL_PATH}")

    url_path, dashboard, config = target
    key = url_path or "default"
    views, did = _sync_views(
        list(config.get("views") or []),
        memory.setdefault(key, {}),
        list(legacy.get(key) or []),
        created=bool(done),
        resolve=_solver_resolver(hass),
    )
    if views != config.get("views") or done:
        new_config = dict(config)
        new_config["views"] = views
        await dashboard.async_save(new_config)
    done += [f"{d}@{key}" for d in did]
    if json.dumps(remembered, sort_keys=True) != before:
        await store.async_save(remembered)
    if done:
        _LOGGER.info("Nimbus dashboard: %s", ", ".join(done))
    return done
