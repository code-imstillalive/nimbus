"""The hub's selects: household mode and the load-forecast policy, the
pricing source, and the device setup mode (#1837).

Covers what lived in #1574/DC-004 (mode refusals), the pricing-source
switch-over and the select files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sel = importlib.import_module("custom_components.nimbus_load.select")
const = importlib.import_module("custom_components.nimbus_load.const")


class MemoryStore:
    def __init__(self, data=None, broken=False):
        self.data = data
        self.broken = broken

    async def async_load(self):
        if self.broken:
            raise ValueError("corrupt")
        return self.data

    async def async_save(self, data):
        self.data = data


async def _value(v):
    return v


def _run(coro):
    return asyncio.run(coro)


def _entity(entity, hass=None):
    entity.hass = hass
    entity.writes = []
    entity.async_write_ha_state = lambda: entity.writes.append(
        entity._attr_current_option
    )
    return entity


# --- household mode and the load-forecast policy ----------------------------------


def _mode(*, restored=None, stored=None):
    shared = sel._SharedSelectStore(store=MemoryStore(stored))
    s = sel.NimbusSelect(
        SimpleNamespace(entry_id="hub1", options={}),
        const.CONF_HOUSEHOLD_MODE,
        "Household Mode",
        list(const.HOUSEHOLD_MODES),
        const.DEFAULT_HOUSEHOLD_MODE,
        "1",
        shared,
    )
    s.async_get_last_state = lambda: _value(
        None if restored is None else SimpleNamespace(state=restored)
    )
    return _entity(s), shared


OTHER = next(m for m in const.HOUSEHOLD_MODES if m != const.DEFAULT_HOUSEHOLD_MODE)


def test_a_select_comes_back_from_its_store_then_its_state_then_the_default():
    # test_a_select_comes_back_from_its_store_then_its_state_then_the_default
    for restored, stored, current in [
        (None, None, const.DEFAULT_HOUSEHOLD_MODE),
        (OTHER, None, OTHER),
        ("retired-mode", None, const.DEFAULT_HOUSEHOLD_MODE),
        (
            const.DEFAULT_HOUSEHOLD_MODE,
            {const.CONF_HOUSEHOLD_MODE: {"value": OTHER, "written_at": 1}},
            OTHER,
        ),
        (
            OTHER,
            {const.CONF_HOUSEHOLD_MODE: {"value": "retired-mode", "written_at": 1}},
            OTHER,
        ),
        (
            None,
            {const.CONF_HOUSEHOLD_MODE: {"value": OTHER}},
            const.DEFAULT_HOUSEHOLD_MODE,
        ),
    ]:
        s, _ = _mode(restored=restored, stored=stored)
        _run(s.async_added_to_hass())
        assert s._attr_current_option == current
        assert s.entity_id == "select.nimbus_household_mode"

    # test_choosing_stores_it_and_an_unknown_choice_is_refused
    s, shared = _mode()
    _run(s.async_select_option(OTHER))
    assert (
        s.writes == [OTHER]
        and shared.store.data[const.CONF_HOUSEHOLD_MODE]["value"] == OTHER
    )
    with pytest.raises(ValueError, match="not a valid Nimbus"):
        _run(s.async_select_option("party"))
    assert s._attr_current_option == OTHER

    # test_a_corrupt_select_store_reads_as_empty
    shared = sel._SharedSelectStore(store=MemoryStore(broken=True))
    assert _run(shared.async_read("k")) is None
    _run(shared.async_write("k", "x"))
    assert shared.store.data["k"]["value"] == "x"


# --- the pricing source -------------------------------------------------------------


@pytest.fixture
def pricing(monkeypatch):
    world = {"profiles": {"Amber": {"id": "amber"}}, "problems": []}
    ps = sel.pricing_source
    monkeypatch.setattr(ps, "detected_profiles", lambda hass: dict(world["profiles"]))
    monkeypatch.setattr(
        ps, "active_label", lambda options, profiles: options.get("label", ps.MANUAL)
    )
    monkeypatch.setattr(
        ps, "status", lambda options, profiles: {"profiles": sorted(profiles)}
    )
    monkeypatch.setattr(ps, "problems", lambda hass, profile: list(world["problems"]))
    monkeypatch.setattr(
        ps, "applied", lambda options, profile: {**options, "label": "Amber"}
    )
    updated = []
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry, options: updated.append(options)
        )
    )
    entry = SimpleNamespace(entry_id="hub1", options={})
    s = _entity(sel.NimbusPricingSourceSelect(hass, entry, "1"), hass)
    return s, world, updated


def test_choosing_a_detected_source_applies_its_profile(pricing):
    s, _, updated = pricing
    _run(s.async_select_option("Amber"))
    assert updated == [{"label": "Amber"}]


def test_a_source_with_problems_or_no_longer_detected_changes_nothing(pricing):
    s, world, updated = pricing
    world["problems"] = ["sensor.amber_general_price is unavailable"]
    with pytest.raises(sel.HomeAssistantError, match="nothing changed. sensor.amber"):
        _run(s.async_select_option("Amber"))
    with pytest.raises(sel.HomeAssistantError, match="does not detect Flow"):
        _run(s.async_select_option("Flow"))
    assert updated == []
    _run(s.async_select_option(sel.pricing_source.MANUAL))
    assert s._attr_current_option == sel.pricing_source.MANUAL and updated == []


# --- the device setup mode ------------------------------------------------------------


@pytest.fixture
def setup_mode(monkeypatch):
    dc = importlib.import_module("custom_components.nimbus_load.device_contract_ha")
    world = {"mode": "legacy", "result": {"ok": True}, "issues": {}}

    async def preview(hass, entry):
        return {
            "mode": world["mode"],
            "revision": 3,
            "clean": False,
            "effective_diff": [1, 2],
            "conflicts": [1],
            "problems": [],
            "devices": [1, 2, 3],
        }

    async def set_mode(hass, entry, option):
        if world["result"].get("ok"):
            world["mode"] = option
        return world["result"]

    monkeypatch.setattr(dc, "async_preview", preview)
    monkeypatch.setattr(dc, "async_set_mode", set_mode)
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.async_get = lambda hass: SimpleNamespace(issues=world["issues"])
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.issue_registry", issue_registry
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "issue_registry",
        issue_registry,
        raising=False,
    )
    s = _entity(
        sel.NimbusDeviceSetupModeSelect(SimpleNamespace(entry_id="hub1"), "1"),
        SimpleNamespace(),
    )
    return s, world, dc


def test_choosing_a_mode_switches_it_or_says_why_not(setup_mode):
    s, world, _ = setup_mode
    _run(s._async_refresh())
    _run(s.async_select_option("legacy"))  # already there: nothing to do
    assert s.writes == []
    _run(s.async_select_option("shadow"))
    assert s._attr_current_option == "shadow"
    world["result"] = {
        "ok": False,
        "code": "dry_run_not_clean",
        "effective_diff": [1],
        "conflicts": [],
        "problems": [1, 2],
    }
    with pytest.raises(
        sel.HomeAssistantError,
        match=r"1 setting\(s\) would change, 0 conflict\(s\), 2 problem\(s\)",
    ):
        _run(s.async_select_option("active"))
    assert s._attr_current_option == "shadow"
    world["result"] = {
        "ok": False,
        "code": "units_undeclared",
        "sensors": ["sensor.a", "sensor.b"],
    }
    with pytest.raises(sel.HomeAssistantError, match="sensor.a, sensor.b"):
        _run(s.async_select_option("active"))
    world["result"] = {"ok": False, "code": "something_new"}
    with pytest.raises(sel.HomeAssistantError, match="something_new"):
        _run(s.async_select_option("active"))
