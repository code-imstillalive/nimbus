"""The standard tabs of the Nimbus dashboard (nimbus #1529, #1543), built on
request and never written by Nimbus (HACS review, hacs/default#12099).

The tabs, in this order:

    Forecaster, Solver, Topology, Control Panel, Regret, Help

Every card discovers its own entities, and the Solver and Help tabs
(`solver_tab.py`, `help_tab.py`) are resolved against this install's own
entity registry, so the YAML is this install's, ready to paste.

Nimbus does not create, update or rewrite any dashboard
---------------------------------------------------------
Until this change (26 Oct 2026) Nimbus kept these tabs on "the Nimbus dashboard" at every
start: it picked a storage dashboard by a url path or title containing
"nimbus" (so possibly one the household built), rewrote its views, and with
none it created a sidebar dashboard through Lovelace's private storage
collection. The HACS store review (Frenck, 24 Oct 2026) asked for that to
go: "An integration should not write the user's Home Assistant
configuration on its own, and never into objects it did not create." Home
Assistant has no public API for an integration to create a dashboard, so an
explicit "create it for me" step would still need the private collection.
The household's decision, 26 Oct 2026: "definitely do what Frenck wants".

What is left is the explicit, read-only half: `nimbus_load.get_dashboard`
(an action that returns this install's dashboard and writes nothing), and
the copy-paste dashboard in docs/dashboards.md. A dashboard Nimbus created or
filled before this release is left exactly as it is.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

try:
    from . import help_tab, solver_tab
except ImportError:  # pragma: no cover - loaded as a top-level module in tests
    import help_tab  # type: ignore[no-redef]
    import solver_tab  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

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
    {
        # nimbus #1823: setting Nimbus up so every tab works, and what every
        # figure and setting means. Built from help_tab.template() and
        # resolved like the Solver tab, so its live ticks are this install's.
        "key": "help",
        "title": "Help",
        "path": "help",
        "sections_template": "help",
        "match": ("sensor.nimbus_solver_lp_status",),
    },
)

#: Which module builds each templated tab's sections.
_TEMPLATES = {True: solver_tab.template, "help": help_tab.template}

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
            "sections": _TEMPLATES[spec["sections_template"]](),
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


def _has_lovelace_resource(hass: HomeAssistant, name: str) -> bool:
    """Whether a dashboard resource whose URL contains `name` is installed
    (read only). The Solver tab uses the household's mushroom sliders where
    mushroom is installed and HA's own tile slider everywhere else."""
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        items = resources.async_items() if resources is not None else []
        return any(name in str(item.get("url", "")) for item in items)
    except Exception:  # noqa: BLE001 -- the tile slider is always a safe answer
        return False


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
    # A setting that moved platform (the reserve's window, number -> time,
    # 11 Oct 2026) can briefly have both registry rows under one unique id
    # until time.py removes the number's: the time entity wins, or the tab
    # is written against the entity about to be deleted ("Entity not found").
    by_uid: dict[str, str] = {}
    name_by_uid: dict[str, str] = {}
    for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if e.unique_id in by_uid and e.domain != "time":
            continue
        by_uid[e.unique_id] = e.entity_id
        name_by_uid[e.unique_id] = e.name or e.original_name or e.entity_id

    mushroom = _has_lovelace_resource(hass, "mushroom")

    def lookup(key: str) -> str | None:
        if key == "card:mushroom":
            return "custom:mushroom-number-card" if mushroom else None
        if key.startswith("name:"):
            # `${@name:<key>}`: that entity's name, for a chart series
            # labelled per install (one SoC line per battery).
            return name_by_uid.get(f"{entry.entry_id}_{key.split(':', 1)[1]}")
        if key.startswith("option:"):
            value = entry.options.get(key.split(":", 1)[1])
            return value if isinstance(value, str) and value else None
        return by_uid.get(f"{entry.entry_id}_{key}")

    return lambda view: solver_tab.resolve(view, lookup)


def dashboard_config(resolve: Any = None) -> dict[str, Any]:
    """The Nimbus dashboard, every standard tab in order. `resolve` binds the
    Solver and Help tabs to an install's entities; without it those two tabs
    are left out (they would be placeholders). Pure: builds, writes nothing."""
    views = []
    for spec in STANDARD_VIEWS:
        view = _view_for(spec)
        if spec.get("sections_template"):
            if resolve is None:
                continue
            view = resolve(view)
        views.append(view)
    return {"title": DASHBOARD_TITLE, "views": views}


async def async_dashboard_config(hass: HomeAssistant) -> dict[str, Any]:
    """This install's Nimbus dashboard, for `nimbus_load.get_dashboard`.
    Reads the entity registry and, off the event loop, the Help tab's
    strings. Writes nothing."""
    await hass.async_add_executor_job(help_tab._strings)
    return dashboard_config(_solver_resolver(hass))
