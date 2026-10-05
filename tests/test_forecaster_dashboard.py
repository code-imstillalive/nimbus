"""nimbus #1529: the Forecaster view is added automatically, safely.

`forecaster_dashboard.async_register_forecaster_dashboard` must put a
"Nimbus Forecaster" sidebar dashboard in front of every household, and must
never touch a dashboard the household owns. These tests drive it against
small fakes of the three Home Assistant internals it uses
(`frontend.async_panel_exists` / `async_register_built_in_panel`,
`lovelace.const.LOVELACE_DATA` / `ConfigNotFound`,
`lovelace.dashboard.LovelaceStorage`), whose shapes were read from HA
2026.7.4 and 2026.9.3 and are identical in both.
"""

from __future__ import annotations

import asyncio
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


class _FakeStorage:
    saved: ClassVar[dict[str, dict]] = {}

    def __init__(self, hass, config):
        self.config = config
        self.key = config["id"]

    async def async_load(self, force):
        if self.key not in _FakeStorage.saved:
            raise _ConfigNotFound
        return _FakeStorage.saved[self.key]

    async def async_save(self, config):
        _FakeStorage.saved[self.key] = config


@pytest.fixture
def ha(monkeypatch):
    panels: dict[str, dict] = {}
    lovelace_key = object()

    frontend = types.ModuleType("homeassistant.components.frontend")
    frontend.async_panel_exists = lambda hass, url: url in panels

    def register(hass, component, **kw):
        if kw["frontend_url_path"] in panels:
            raise ValueError("Overwriting panel")
        panels[kw["frontend_url_path"]] = {"component": component, **kw}

    frontend.async_register_built_in_panel = register
    const = types.ModuleType("homeassistant.components.lovelace.const")
    const.LOVELACE_DATA = lovelace_key
    const.ConfigNotFound = _ConfigNotFound
    dashboard = types.ModuleType("homeassistant.components.lovelace.dashboard")
    dashboard.LovelaceStorage = _FakeStorage
    for name, mod in (
        ("homeassistant.components.frontend", frontend),
        ("homeassistant.components.lovelace", types.ModuleType("x")),
        ("homeassistant.components.lovelace.const", const),
        ("homeassistant.components.lovelace.dashboard", dashboard),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    _FakeStorage.saved = {}
    hass = types.SimpleNamespace(
        data={lovelace_key: types.SimpleNamespace(dashboards={None: object()})}
    )
    return types.SimpleNamespace(hass=hass, panels=panels, key=lovelace_key)


def test_creates_and_seeds_the_forecaster_dashboard(ha) -> None:
    mod = _load_module()
    assert asyncio.run(mod.async_register_forecaster_dashboard(ha.hass)) is True
    panel = ha.panels[mod.URL_PATH]
    assert panel["component"] == "lovelace"
    assert panel["sidebar_title"] == "Nimbus Forecaster"
    assert panel["config"] == {"mode": "storage"}
    assert panel["require_admin"] is False
    saved = _FakeStorage.saved[mod.STORAGE_ID]
    cards = saved["views"][0]["cards"]
    assert cards == [{"type": "custom:nimbus-forecast-card"}]
    assert mod.URL_PATH in ha.hass.data[ha.key].dashboards


def test_never_overwrites_a_households_edits(ha) -> None:
    mod = _load_module()
    edited = {"views": [{"title": "Mine", "cards": []}]}
    _FakeStorage.saved[mod.STORAGE_ID] = edited
    asyncio.run(mod.async_register_forecaster_dashboard(ha.hass))
    assert _FakeStorage.saved[mod.STORAGE_ID] is edited


def test_leaves_an_existing_dashboard_or_panel_alone(ha) -> None:
    mod = _load_module()
    ha.panels[mod.URL_PATH] = {"component": "someone_elses"}
    assert asyncio.run(mod.async_register_forecaster_dashboard(ha.hass)) is False
    assert ha.panels[mod.URL_PATH] == {"component": "someone_elses"}
    assert _FakeStorage.saved == {}


def test_skips_quietly_without_lovelace(ha) -> None:
    mod = _load_module()
    ha.hass.data.pop(ha.key)
    assert asyncio.run(mod.async_register_forecaster_dashboard(ha.hass)) is False
    assert ha.panels == {}


def test_registers_once_per_process(ha) -> None:
    mod = _load_module()
    assert asyncio.run(mod.async_register_forecaster_dashboard(ha.hass)) is True
    # A second call (another config-entry reload) must not raise
    # "Overwriting panel" or touch anything.
    assert asyncio.run(mod.async_register_forecaster_dashboard(ha.hass)) is False


def test_setup_calls_it_and_tolerates_failure() -> None:
    init = (MODULE_PATH.parent / "__init__.py").read_text(encoding="utf-8")
    call = init.index("await async_register_forecaster_dashboard(hass)")
    block = init[init.rindex("try:", 0, call) : init.index("except Exception:", call)]
    assert "async_register_forecaster_dashboard" in block
    manifest = (MODULE_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    assert '"lovelace"' in manifest
