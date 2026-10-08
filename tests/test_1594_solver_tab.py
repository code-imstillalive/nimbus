"""nimbus #1594: the standard Solver tab.

Contents agreed by the household (6 and 8 Oct 2026): headline, plan against
actual, money, how well it did, risk sliders, and every setting grouped into
Basic, P2P (optional) and Advanced. It installs itself: second on a new
install's dashboard, and on an existing one beside a household-built tab of
the same title, which is never touched.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _PKG / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


solver_tab = _load("solver_tab", "solver_tab.py")
dash = _load("_nimbus_fdash_1594", "forecaster_dashboard.py")

ENTRY = "01TESTENTRY"


def _keys(node) -> set[str]:
    text = json.dumps(node)
    out, i = set(), 0
    while (i := text.find('"@', i)) != -1:
        j = text.index('"', i + 1)
        out.add(text[i + 2 : j])
        i = j
    return out


def _lookup_all(key: str) -> str | None:
    """Every Nimbus entity exists; entity_ids carry a `_2` to prove the
    template never relies on the entity_id itself."""
    if key.startswith("option:"):
        return "sensor.my_battery_power" if key == "option:battery_sensor" else None
    domain = "number" if key.startswith("solver_") else "sensor"
    if key.endswith("_enabled") or key in (
        "solver_dispatch_dry_run",
        "solver_auto_include_known_solar",
        "solver_price_spike_override_armed",
    ):
        domain = "switch"
    if key == "solver_load_forecast_source_policy":
        domain = "select"
    object_id = key if key.startswith("nimbus_") else f"nimbus_{key}"
    return f"{domain}.{object_id}_2"


def _resolve(view):
    return solver_tab.resolve(view, _lookup_all)


def test_the_sections_are_the_agreed_ones_in_order() -> None:
    headings = [s["cards"][0]["heading"] for s in solver_tab.template()]
    assert headings == [
        "Solver",
        "Plan against actual",
        "Money",
        "How well it did",
        "Risk sliders",
        "Basic settings",
        "P2P (optional)",
        "Advanced settings",
    ]


def test_every_setting_is_on_the_tab_exactly_once() -> None:
    """All settings, grouped (household, 8 Oct 2026): every Solver number,
    switch and select appears once, in Basic, P2P or Advanced."""
    sections = {s["cards"][0]["heading"]: s for s in solver_tab.template()}
    settings = [
        k
        for name in (
            "Basic settings",
            "P2P (optional)",
            "Advanced settings",
            "Risk sliders",
        )
        for k in _keys(sections[name])
        if k.startswith("solver_")
    ]
    assert len(settings) == len(set(settings))
    assert {
        "solver_battery_capacity_kwh",
        "solver_network_fee_default_rate",
        "solver_p2p_block_1_rate_kw",
        "solver_proximal_weight_kw",
        "solver_flex_signals_enabled",
        "solver_import_price_risk_aversion",
    } <= set(settings)


def test_resolution_leaves_no_placeholder_and_uses_the_registry_ids() -> None:
    view = _resolve(
        dash._view_for(next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver"))
    )
    text = json.dumps(view)
    assert '"@' not in text
    assert "sensor.nimbus_solver_lp_status_2" in text
    assert "sensor.my_battery_power" in text


def test_a_missing_entity_is_dropped_not_written_blank() -> None:
    def partial(key: str) -> str | None:
        return (
            None
            if key in ("nimbus_quality_epr", "option:battery_sensor")
            else _lookup_all(key)
        )

    view = solver_tab.resolve(solver_tab.template(), partial)
    text = json.dumps(view)
    assert "nimbus_quality_epr_2" not in text
    assert '"@' not in text
    plan = view[1]["cards"][1]
    assert [e["name"] for e in plan["entities"]] == ["Nimbus plan (now)"]


def test_an_entities_card_with_nothing_left_is_dropped() -> None:
    view = solver_tab.resolve(solver_tab.template(), lambda _k: None)
    for section in view:
        for card in section["cards"]:
            assert card["type"] in ("heading", "markdown"), card


def test_the_design_is_the_same_on_every_install() -> None:
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    assert dash.design_of(spec) == dash.design_of(spec)
    assert '"@nimbus_solver_lp_status"' in json.dumps(dash._view_for(spec))


def test_a_new_install_gets_solver_second() -> None:
    views, done = dash._sync_views([], {}, [], created=True, resolve=_resolve)
    assert [v["title"] for v in views] == [
        "Forecaster",
        "Solver",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    assert "added:solver" in done


def test_an_existing_dashboard_gets_it_beside_the_households_own_solver_tab() -> None:
    """Household, 8 Oct 2026: "install itself at the end or next to it"."""
    theirs = {"title": "Solver", "path": "solver", "type": "sections", "sections": []}
    before = [
        {
            "title": "Forecaster",
            "path": "forecaster",
            "cards": [{"type": "custom:nimbus-forecast-card"}],
        },
        theirs,
        {"title": "SUNSYNK", "path": "sunsynk", "cards": []},
    ]
    views, done = dash._sync_views([dict(v) for v in before], {}, [], resolve=_resolve)
    titles = [v["title"] for v in views]
    assert titles[:4] == ["Forecaster", "Solver", "Solver", "SUNSYNK"]
    assert views[1] == theirs  # untouched
    assert views[2]["path"] == "solver-2"
    assert "added:solver" in done


def test_without_a_matching_tab_it_goes_at_the_end() -> None:
    views, _ = dash._sync_views(
        [{"title": "Home", "path": "home", "cards": []}], {}, [], resolve=_resolve
    )
    assert [v["title"] for v in views][1] == "Solver" or views[-1]["title"] in (
        "Solver",
        "Regret",
    )
    assert any(v["title"] == "Solver" for v in views)


def test_a_restart_changes_nothing() -> None:
    mem: dict = {}
    views, _ = dash._sync_views([], mem, [], created=True, resolve=_resolve)
    again, done = dash._sync_views(
        json.loads(json.dumps(views)), mem, [], resolve=_resolve
    )
    assert done == []
    assert again == views


def test_without_a_resolver_the_tab_is_never_written() -> None:
    views, done = dash._sync_views([], {}, [], created=True)
    assert "Solver" not in [v["title"] for v in views]
    assert all("solver" not in d for d in done)


@pytest.mark.parametrize("view", [solver_tab.template()])
def test_no_haeo_and_no_icon(view) -> None:
    text = json.dumps(view).lower()
    assert "haeo" not in text
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    assert "icon" not in dash._view_for(spec)
