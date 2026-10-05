"""nimbus #1529: the Forecaster tab is added to the Nimbus dashboard, safely.

`forecaster_dashboard.async_add_forecaster_view` appends a Forecaster view
to each storage dashboard that already holds Nimbus's own cards, next to
its Control Panel / Topology / Regret views. It must change nothing else,
never duplicate, add once per dashboard, and skip YAML dashboards.

Driven against small fakes of the Home Assistant internals it uses
(`lovelace.const.LOVELACE_DATA` / `ConfigNotFound`, a storage dashboard's
`async_load` / `async_save` / `mode`, and `helpers.storage.Store`), whose
shapes were read from HA 2026.7.4 and 2026.9.3 and are identical in both.
"""

from __future__ import annotations

import asyncio
import copy
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
        {
            "title": "Topology",
            "path": "topology",
            "cards": [{"type": "custom:nimbus-topology-card"}],
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
    dashboards = {
        "dashboard-nimbus": _FakeDashboard(copy.deepcopy(NIMBUS_DASH)),
        None: _FakeDashboard(copy.deepcopy(OTHER_DASH)),
    }
    hass = types.SimpleNamespace(
        data={key: types.SimpleNamespace(dashboards=dashboards)}
    )
    return types.SimpleNamespace(hass=hass, dashboards=dashboards, key=key)


def _run(mod, hass):
    return asyncio.run(mod.async_add_forecaster_view(hass))


def test_adds_a_forecaster_tab_to_the_nimbus_dashboard_only(ha) -> None:
    mod = _load_module()
    assert _run(mod, ha.hass) == ["dashboard-nimbus"]
    views = ha.dashboards["dashboard-nimbus"].config["views"]
    assert [v["title"] for v in views] == [
        "Control Panel",
        "Regret",
        "Topology",
        "Forecaster",
    ]
    assert views[-1]["cards"] == [{"type": "custom:nimbus-forecast-card"}]
    # A dashboard with no Nimbus card is not touched.
    assert ha.dashboards[None].saves == 0


def test_changes_nothing_else(ha) -> None:
    mod = _load_module()
    _run(mod, ha.hass)
    views = ha.dashboards["dashboard-nimbus"].config["views"]
    assert views[:3] == NIMBUS_DASH["views"]  # card_mod and all, untouched
    assert ha.dashboards["dashboard-nimbus"].config["title"] == "Nimbus"


def test_never_duplicates_the_card(ha) -> None:
    mod = _load_module()
    own = copy.deepcopy(NIMBUS_DASH)
    own["views"][0]["cards"].append({"type": "custom:nimbus-forecast-card"})
    ha.dashboards["dashboard-nimbus"].config = own
    assert _run(mod, ha.hass) == []
    assert ha.dashboards["dashboard-nimbus"].saves == 0


def test_own_forecaster_view_gets_a_nimbus_forecaster_tab_beside_it(ha) -> None:
    """Household decision, 5 Oct 2026: a household's own "Forecaster" view
    (its own charts, not this card) is never touched, and the new tab is
    added beside it as "Nimbus Forecaster"."""
    mod = _load_module()
    own = copy.deepcopy(NIMBUS_DASH)
    mine = {
        "title": "Forecaster",
        "path": "forecaster",
        "cards": [{"type": "custom:apexcharts-card"}],
    }
    own["views"].insert(0, mine)
    ha.dashboards["dashboard-nimbus"].config = own
    assert _run(mod, ha.hass) == ["dashboard-nimbus"]
    views = ha.dashboards["dashboard-nimbus"].config["views"]
    assert views[0] == mine
    assert views[-1]["title"] == "Nimbus Forecaster"
    assert views[-1]["path"] == "nimbus-forecaster"
    assert views[-1]["cards"] == [{"type": "custom:nimbus-forecast-card"}]


def test_once_per_dashboard_even_after_the_tab_is_deleted(ha) -> None:
    mod = _load_module()
    _run(mod, ha.hass)
    dash = ha.dashboards["dashboard-nimbus"]
    dash.config["views"] = dash.config["views"][:3]  # household deletes the tab
    assert _run(mod, ha.hass) == []
    assert [v["title"] for v in dash.config["views"]] == [
        "Control Panel",
        "Regret",
        "Topology",
    ]


def test_skips_yaml_dashboards(ha) -> None:
    mod = _load_module()
    ha.dashboards["dashboard-nimbus"].mode = "yaml"
    assert _run(mod, ha.hass) == []
    assert ha.dashboards["dashboard-nimbus"].saves == 0


def test_does_not_collide_with_an_existing_forecaster_path(ha) -> None:
    mod = _load_module()
    ha.dashboards["dashboard-nimbus"].config["views"][0]["path"] = "forecaster"
    _run(mod, ha.hass)
    assert (
        ha.dashboards["dashboard-nimbus"].config["views"][-1]["path"]
        == "nimbus-forecaster"
    )


def test_skips_quietly_without_lovelace(ha) -> None:
    mod = _load_module()
    ha.hass.data.pop(ha.key)
    assert _run(mod, ha.hass) == []


def test_setup_calls_it_and_tolerates_failure() -> None:
    init = (MODULE_PATH.parent / "__init__.py").read_text(encoding="utf-8")
    call = init.index("await async_add_forecaster_view(hass)")
    assert init.rindex("try:", 0, call) < call < init.index("except Exception:", call)
    manifest = (MODULE_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    assert '"lovelace"' in manifest
