"""nimbus #1529/#1543: the standard tabs of the Nimbus dashboard.

Rules agreed by the household and Mark Purcell, 6 Oct 2026 (see the module
doc of forecaster_dashboard.py): a new install gets a Nimbus dashboard; a
missing tab is added; an untouched tab is updated in place on a design
change; a changed or household-built tab is never touched and the new design
goes beside it under the same plain title; only on a real design change; a
deleted tab comes back once, on a design change; nothing of the household's
is ever deleted, edited or reordered; every tab titled, never an icon, never
"Nimbus" in a title.

Driven against small fakes of the Home Assistant internals it uses
(`lovelace.const.LOVELACE_DATA`, a dashboard's `async_load` / `async_save` /
`mode`, `helpers.storage.Store`, and the `lovelace/dashboards/create`
websocket handler wrapped the way HA wraps it), whose shapes were read from
HA 2026.7.4 and 2026.9.3 and are identical in both.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import importlib.util
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nimbus_load"
    / "forecaster_dashboard.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_nimbus_fdash", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ConfigNotFound(Exception):
    pass


class _FakeDashboard:
    def __init__(self, config, mode="storage"):
        self.config = config
        self.mode = mode
        self.saves = 0

    async def async_load(self, force):
        if self.config is None:
            raise _ConfigNotFound
        return copy.deepcopy(self.config)

    async def async_save(self, config):
        self.config = config
        self.saves += 1


class _FakeStore:
    data: ClassVar[dict] = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return _FakeStore.data.get(self.key)

    async def async_save(self, data):
        _FakeStore.data[self.key] = data


class _FakeCollection:
    """The storage dashboards collection: creating an item fires the
    lovelace listener, which registers an empty storage dashboard."""

    def __init__(self, dashboards):
        self.dashboards = dashboards
        self.created = []

    async def async_create_item(self, item):
        self.created.append(item)
        self.dashboards[item["url_path"]] = _FakeDashboard(None)
        return item


class _FakeCollectionWebSocket:
    def __init__(self, collection):
        self.storage_collection = collection

    def ws_create_item(self, hass, connection, msg):
        raise AssertionError("never called: only used to find the collection")


def _ha_wrapped(func):
    """require_admin(async_response(func)), as HA registers create."""

    @functools.wraps(func)
    def schedule_handler(hass, connection, msg):
        return None

    @functools.wraps(schedule_handler)
    def with_admin(hass, connection, msg):
        return None

    return with_admin


NIMBUS_DASH = {
    "title": "Nimbus",
    "views": [
        {
            "title": "Control Panel",
            "path": "control",
            "cards": [
                {"type": "custom:nimbus-dispatch-card-v4", "card_mod": {"style": "x"}}
            ],
        },
        {
            "title": "Regret",
            "path": "regret",
            "cards": [{"type": "custom:nimbus-regret-card"}],
        },
    ],
}
OTHER_DASH = {"views": [{"title": "Home", "cards": [{"type": "entities"}]}]}


@pytest.fixture
def ha(monkeypatch):
    key = object()
    const = types.ModuleType("homeassistant.components.lovelace.const")
    const.LOVELACE_DATA = key
    const.ConfigNotFound = _ConfigNotFound
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _FakeStore
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.lovelace", types.ModuleType("x")
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.lovelace.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    _FakeStore.data = {}
    # A new install: only HA's own default dashboard, no Nimbus card anywhere.
    dashboards = {None: _FakeDashboard(copy.deepcopy(OTHER_DASH))}
    collection = _FakeCollection(dashboards)
    ws = _FakeCollectionWebSocket(collection)
    hass = types.SimpleNamespace(
        data={
            key: types.SimpleNamespace(dashboards=dashboards),
            "websocket_api": {
                "lovelace/dashboards/create": (_ha_wrapped(ws.ws_create_item), None)
            },
        }
    )
    return types.SimpleNamespace(
        hass=hass, dashboards=dashboards, key=key, collection=collection
    )


def _run(mod, hass):
    return asyncio.run(mod.async_ensure_nimbus_dashboard(hass))


def _cards(view):
    return [c["type"] for s in view["sections"] for c in s["cards"]]


def _views(ha, url="dashboard-nimbus"):
    return ha.dashboards[url].config["views"]


def _titles(ha, url="dashboard-nimbus"):
    return [v.get("title") for v in _views(ha, url)]


def _redesign(mod, monkeypatch, key):
    """A later release changes Nimbus's own design of one tab."""
    specs = []
    for spec in mod.STANDARD_VIEWS:
        if spec["key"] == key:
            spec = {**spec, "cards": ({"redesigned": True},)}
        specs.append(spec)
    monkeypatch.setattr(mod, "STANDARD_VIEWS", tuple(specs))


# ---------------------------------------------------------------- new install


def test_a_new_install_gets_the_four_standard_tabs_in_order(ha) -> None:
    mod = _load_module()
    done = _run(mod, ha.hass)
    assert done[0] == "created:dashboard-nimbus"
    assert ha.collection.created == [
        {
            "title": "Nimbus",
            "url_path": "dashboard-nimbus",
            "require_admin": False,
            "show_in_sidebar": True,
        }
    ]
    assert _titles(ha) == ["Forecaster", "Topology", "Control Panel", "Regret"]
    assert [_cards(v) for v in _views(ha)] == [
        ["custom:nimbus-forecast-card", "custom:nimbus-forecast-card"],
        ["custom:nimbus-topology-card"],
        ["custom:nimbus-dispatch-card-v4"],
        ["custom:nimbus-regret-card"],
    ]
    assert ha.dashboards[None].saves == 0  # HA's own dashboard never touched


def test_every_tab_is_titled_never_an_icon_never_nimbus(ha) -> None:
    """Household, 6 Oct 2026: "every view must get TITLED, no icons", and no
    "Nimbus" in a tab title -- the tabs are on the Nimbus dashboard already."""
    mod = _load_module()
    _run(mod, ha.hass)
    assert "icon" not in ha.collection.created[0]
    for view in _views(ha):
        assert view["title"]
        assert "icon" not in view
        assert "nimbus" not in view["title"].lower()
    for spec in mod.STANDARD_VIEWS:
        assert "icon" not in spec


def test_every_tab_is_a_sections_view_with_full_width_cards(ha) -> None:
    mod = _load_module()
    _run(mod, ha.hass)
    for view in _views(ha):
        assert view["type"] == "sections"
        assert view["max_columns"] == 4
        assert "cards" not in view
        (section,) = view["sections"]
        assert section["column_span"] == 4
        for card in section["cards"]:
            assert card["grid_options"] == {"columns": "full", "rows": "auto"}
    assert [c["chart"] for c in _views(ha)[0]["sections"][0]["cards"]] == [
        "signals",
        "loads",
    ]


def test_no_card_carries_household_config(ha) -> None:
    """Every card discovers its own entities; a new user configures nothing."""
    mod = _load_module()
    _run(mod, ha.hass)
    for view in _views(ha):
        for card in view["sections"][0]["cards"]:
            assert set(card) <= {"type", "chart", "grid_options"}


def test_a_restart_changes_nothing(ha) -> None:
    mod = _load_module()
    _run(mod, ha.hass)
    saves = ha.dashboards["dashboard-nimbus"].saves
    assert _run(mod, ha.hass) == []
    assert ha.dashboards["dashboard-nimbus"].saves == saves


def test_the_dashboard_is_created_once_even_after_it_is_deleted(ha) -> None:
    mod = _load_module()
    _run(mod, ha.hass)
    del ha.dashboards["dashboard-nimbus"]  # household deletes the dashboard
    assert _run(mod, ha.hass) == []
    assert "dashboard-nimbus" not in ha.dashboards
    assert len(ha.collection.created) == 1


def test_never_overwrites_a_yaml_dashboard_that_owns_the_path(ha) -> None:
    mod = _load_module()
    theirs = _FakeDashboard(copy.deepcopy(OTHER_DASH), mode="yaml")
    ha.dashboards["dashboard-nimbus"] = theirs
    assert _run(mod, ha.hass) == []
    assert theirs.saves == 0
    assert ha.collection.created == []


def test_an_empty_dashboard_does_not_block_a_new_install(ha) -> None:
    mod = _load_module()
    ha.dashboards["blank"] = _FakeDashboard(None)  # never saved: ConfigNotFound
    assert _run(mod, ha.hass)[0] == "created:dashboard-nimbus"


def test_without_the_collection_nothing_is_created_and_it_retries(ha) -> None:
    mod = _load_module()
    ha.hass.data.pop("websocket_api")
    assert _run(mod, ha.hass) == []
    assert "dashboard-nimbus" not in ha.dashboards
    assert not (_FakeStore.data.get(mod.STORE_KEY) or {}).get("created")


# ------------------------------------------------------- an existing dashboard


def test_a_missing_tab_is_added_and_present_ones_are_left_alone(ha) -> None:
    """Rules 2, 5, 7: Control Panel and Regret are there already (the
    household's own), so only Forecaster and Topology are added, at the end."""
    mod = _load_module()
    mine = _FakeDashboard(copy.deepcopy(NIMBUS_DASH))
    ha.dashboards["my-nimbus"] = mine
    done = _run(mod, ha.hass)
    assert done == ["added:forecaster@my-nimbus", "added:topology@my-nimbus"]
    assert _titles(ha, "my-nimbus") == [
        "Control Panel",
        "Regret",
        "Forecaster",
        "Topology",
    ]
    assert _views(ha, "my-nimbus")[:2] == NIMBUS_DASH["views"]  # card_mod and all
    assert mine.config["title"] == "Nimbus"
    assert ha.collection.created == []


def test_the_old_topology_card_name_counts_as_the_topology_tab(ha) -> None:
    mod = _load_module()
    old = copy.deepcopy(NIMBUS_DASH)
    old["views"].append(
        {"title": "Topology", "cards": [{"type": "custom:switchboard-topology-card"}]}
    )
    ha.dashboards["my-nimbus"] = _FakeDashboard(old)
    done = _run(mod, ha.hass)
    assert "added:topology@my-nimbus" not in done
    assert _titles(ha, "my-nimbus").count("Topology") == 1


def test_the_reference_households_dashboard_gets_nothing(ha) -> None:
    """Replays production's Nimbus dashboard shape, read 6 Oct 2026: every
    standard card already on it (Topology under the old name), its own
    restructured Forecaster tab, and v0.94.438's "handled" memory. A deploy
    must change nothing at all."""
    mod = _load_module()
    prod = {
        "title": "NIMBUS",
        "views": [
            {
                "title": "Forecaster",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "custom:nimbus-forecast-card", "chart": "signals"},
                            {"type": "custom:power-flow-card-plus"},
                        ],
                    }
                ],
            },
            {"title": "Solver", "path": "solver", "cards": []},
            {
                "title": "Topology",
                "path": "topology",
                "cards": [{"type": "custom:switchboard-topology-card"}],
            },
            {
                "title": "Control Panel",
                "path": "control-panel",
                "cards": [{"type": "custom:nimbus-dispatch-card-v4", "config": {}}],
            },
            {
                "title": "Regret",
                "path": "regret",
                "cards": [{"type": "custom:nimbus-regret-card"}],
            },
            {"title": "Overview", "icon": "mdi:sun-compass", "cards": []},
        ],
    }
    _FakeStore.data[mod.STORE_KEY] = {"handled": {"dashboard-nimbus": ["forecaster"]}}
    ha.dashboards["dashboard-nimbus"] = _FakeDashboard(copy.deepcopy(prod))
    assert _run(mod, ha.hass) == []
    assert ha.dashboards["dashboard-nimbus"].saves == 0
    assert ha.dashboards["dashboard-nimbus"].config == prod
    assert ha.collection.created == []


def test_tabs_go_to_the_nimbus_dashboard_not_a_home_dashboard(ha) -> None:
    mod = _load_module()
    home = {
        "title": "Home",
        "views": [{"title": "Home", "cards": [{"type": "custom:nimbus-regret-card"}]}],
    }
    ha.dashboards[None] = _FakeDashboard(copy.deepcopy(home))
    ha.dashboards["energy-nimbus"] = _FakeDashboard(copy.deepcopy(NIMBUS_DASH))
    _run(mod, ha.hass)
    assert ha.dashboards[None].saves == 0
    assert ha.dashboards[None].config == home
    assert "Forecaster" in _titles(ha, "energy-nimbus")


def test_nimbus_cards_only_on_home_get_a_nimbus_dashboard_of_their_own(ha) -> None:
    """Contained (household, 6 Oct 2026): Home is never written, even when
    it is the only dashboard holding Nimbus cards."""
    mod = _load_module()
    home = copy.deepcopy(NIMBUS_DASH)
    home["title"] = "Home"
    ha.dashboards[None] = _FakeDashboard(copy.deepcopy(home))
    done = _run(mod, ha.hass)
    assert done[0] == "created:dashboard-nimbus"
    assert ha.dashboards[None].saves == 0
    assert ha.dashboards[None].config == home
    assert _titles(ha) == ["Forecaster", "Topology", "Control Panel", "Regret"]


def test_only_the_nimbus_dashboard_is_ever_written(ha, monkeypatch) -> None:
    mod = _load_module()
    others = {
        None: copy.deepcopy(OTHER_DASH),
        "energy": {"views": [{"cards": [{"type": "custom:nimbus-forecast-card"}]}]},
    }
    for url, cfg in others.items():
        ha.dashboards[url] = _FakeDashboard(copy.deepcopy(cfg))
    _run(mod, ha.hass)
    _redesign(mod, monkeypatch, "forecaster")
    _run(mod, ha.hass)
    for url, cfg in others.items():
        assert ha.dashboards[url].saves == 0
        assert ha.dashboards[url].config == cfg


def test_a_yaml_dashboard_with_a_nimbus_card_is_never_written(ha) -> None:
    mod = _load_module()
    ha.dashboards["yaml-nimbus"] = _FakeDashboard(
        copy.deepcopy(NIMBUS_DASH), mode="yaml"
    )
    _run(mod, ha.hass)
    assert ha.dashboards["yaml-nimbus"].saves == 0


def test_a_path_the_household_uses_is_never_taken(ha) -> None:
    mod = _load_module()
    own = copy.deepcopy(NIMBUS_DASH)
    own["views"][0]["path"] = "forecaster"  # their Control Panel sits there
    ha.dashboards["my-nimbus"] = _FakeDashboard(own)
    _run(mod, ha.hass)
    added = next(v for v in _views(ha, "my-nimbus") if v["title"] == "Forecaster")
    assert added["path"] == "forecaster-2"


# --------------------------------------------------------- a design change


def test_an_untouched_tab_is_updated_in_place(ha, monkeypatch) -> None:
    """Rule 3 (Mark: "Happy for it to replace if it hasn't been modified")."""
    mod = _load_module()
    _run(mod, ha.hass)
    _redesign(mod, monkeypatch, "regret")
    assert _run(mod, ha.hass) == ["updated:regret@dashboard-nimbus"]
    assert _titles(ha) == ["Forecaster", "Topology", "Control Panel", "Regret"]
    regret = _views(ha)[3]
    assert regret["path"] == "regret"
    assert regret["sections"][0]["cards"][0]["redesigned"] is True
    assert _run(mod, ha.hass) == []  # once


def test_a_changed_tab_is_kept_and_the_new_design_goes_beside_it(
    ha, monkeypatch
) -> None:
    """Rule 4 (household: "if modified add new one next to it for comparison
    by the user"), under the same plain title."""
    mod = _load_module()
    _run(mod, ha.hass)
    mine = _views(ha)[1]
    mine["sections"][0]["cards"].append({"type": "markdown", "content": "mine"})
    kept = copy.deepcopy(mine)
    _redesign(mod, monkeypatch, "topology")
    assert _run(mod, ha.hass) == ["beside:topology@dashboard-nimbus"]
    assert _titles(ha) == [
        "Forecaster",
        "Topology",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    assert _views(ha)[1] == kept  # the household's version, untouched
    assert _views(ha)[2]["path"] == "topology-2"
    assert _views(ha)[2]["sections"][0]["cards"][0]["redesigned"] is True
    assert _run(mod, ha.hass) == []  # once


def test_a_household_built_tab_gets_the_new_design_beside_it(ha, monkeypatch) -> None:
    mod = _load_module()
    mine = _FakeDashboard(copy.deepcopy(NIMBUS_DASH))
    ha.dashboards["my-nimbus"] = mine
    _run(mod, ha.hass)
    assert _titles(ha, "my-nimbus")[:2] == ["Control Panel", "Regret"]
    _redesign(mod, monkeypatch, "control_panel")
    assert _run(mod, ha.hass) == ["beside:control_panel@my-nimbus"]
    assert _titles(ha, "my-nimbus")[:3] == [
        "Control Panel",
        "Control Panel",
        "Regret",
    ]
    assert _views(ha, "my-nimbus")[0] == NIMBUS_DASH["views"][0]


def test_a_deleted_tab_stays_deleted_until_its_design_changes(ha, monkeypatch) -> None:
    """Rule 6: back once, and only on a real design change."""
    mod = _load_module()
    _run(mod, ha.hass)
    dash = ha.dashboards["dashboard-nimbus"]
    dash.config["views"] = [v for v in dash.config["views"] if v["title"] != "Regret"]
    assert _run(mod, ha.hass) == []
    assert "Regret" not in _titles(ha)
    _redesign(mod, monkeypatch, "regret")
    assert _run(mod, ha.hass) == ["beside:regret@dashboard-nimbus"]
    assert _titles(ha)[-1] == "Regret"
    dash.config["views"] = [v for v in dash.config["views"] if v["title"] != "Regret"]
    assert _run(mod, ha.hass) == []  # deleted again: stays deleted


def test_a_design_change_never_edits_or_reorders_other_tabs(ha, monkeypatch) -> None:
    mod = _load_module()
    own = copy.deepcopy(NIMBUS_DASH)
    own["views"].insert(1, {"title": "Mine", "path": "mine", "cards": []})
    ha.dashboards["my-nimbus"] = _FakeDashboard(own)
    _run(mod, ha.hass)
    before = copy.deepcopy(_views(ha, "my-nimbus"))
    _redesign(mod, monkeypatch, "forecaster")
    _run(mod, ha.hass)
    after = _views(ha, "my-nimbus")
    assert [v for v in after if v.get("path") != "forecaster"] == [
        v for v in before if v.get("path") != "forecaster"
    ]


# ------------------------------------------------------------------ plumbing


def test_skips_quietly_without_lovelace(ha) -> None:
    mod = _load_module()
    ha.hass.data.pop(ha.key)
    assert _run(mod, ha.hass) == []


def test_finds_the_collection_through_has_wrappers(ha) -> None:
    mod = _load_module()
    assert mod._dashboards_collection(ha.hass) is ha.collection
    ha.hass.data["websocket_api"] = {}
    assert mod._dashboards_collection(ha.hass) is None


def test_setup_calls_it_and_tolerates_failure() -> None:
    init = (MODULE_PATH.parent / "__init__.py").read_text(encoding="utf-8")
    call = init.index("await async_ensure_nimbus_dashboard(hass)")
    assert init.rindex("try:", 0, call) < call < init.index("except Exception:", call)
    manifest = (MODULE_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    assert '"lovelace"' in manifest


def test_an_untouched_panel_tab_from_an_earlier_release_becomes_sections(ha) -> None:
    """v0.94.438/439 added the tab as a panel view holding just the card. It
    is converted in place, keeping its title and path, once."""
    mod = _load_module()
    old = copy.deepcopy(NIMBUS_DASH)
    old["views"].append(
        {
            "title": "Forecaster Charts",
            "path": "forecaster",
            "type": "panel",
            "cards": [{"type": "custom:nimbus-forecast-card"}],
        }
    )
    _FakeStore.data[mod.STORE_KEY] = {"handled": {"my-nimbus": ["forecaster"]}}
    dash = _FakeDashboard(old)
    ha.dashboards["my-nimbus"] = dash
    _run(mod, ha.hass)
    view = dash.config["views"][2]
    assert view["title"] == "Forecaster Charts"
    assert view["path"] == "forecaster"
    assert view["type"] == "sections"
    assert dash.config["views"][:2] == NIMBUS_DASH["views"]
    saves = dash.saves
    _run(mod, ha.hass)
    assert dash.saves == saves


def test_an_already_handled_install_still_converts_its_old_panel_tab(ha) -> None:
    """IV&V 2026-10-06: faa52f5 removed the `if not pending: continue`
    short-circuit specifically so the panel->sections conversion still runs
    on a dashboard this Store already marks done (every real upgrade of an
    already-running install hits this path, since "forecaster" was recorded
    handled the first time the tab was added, releases ago). The shipped
    test above only covers a FRESH Store, where "forecaster" is still
    pending -- that passes even if the short-circuit were still there, since
    `pending` is non-empty either way. This drives the realistic case: the
    Store pre-populated as already-handled, same as every install that
    upgrades through this release actually is."""
    mod = _load_module()
    old = copy.deepcopy(NIMBUS_DASH)
    old["views"].append(
        {
            "title": "Forecaster Charts",
            "path": "forecaster",
            "type": "panel",
            "cards": [{"type": "custom:nimbus-forecast-card"}],
        }
    )
    dash = _FakeDashboard(old)
    ha.dashboards["dashboard-nimbus"] = dash
    _FakeStore.data = {
        "nimbus_load.forecaster_view": {"handled": {"dashboard-nimbus": ["forecaster"]}}
    }
    _run(mod, ha.hass)
    view = next(v for v in dash.config["views"] if v.get("path") == "forecaster")
    assert view["title"] == "Forecaster Charts"
    assert view["type"] == "sections"
    assert "icon" not in view
    assert [c["chart"] for c in view["sections"][0]["cards"]] == ["signals", "loads"]
    assert dash.saves == 1  # one save, carrying the conversion


def test_an_edited_panel_tab_is_left_alone(ha) -> None:
    mod = _load_module()
    edited = copy.deepcopy(NIMBUS_DASH)
    mine = {
        "title": "Forecaster Charts",
        "path": "forecaster",
        "type": "panel",
        "cards": [
            {
                "type": "grid",
                "cards": [{"type": "custom:nimbus-forecast-card", "chart": "loads"}],
            }
        ],
    }
    edited["views"].append(mine)
    dash = _FakeDashboard(edited)
    ha.dashboards["my-nimbus"] = dash
    _run(mod, ha.hass)
    assert mine in dash.config["views"]
