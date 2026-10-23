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


@pytest.mark.parametrize(
    ("restored", "stored", "current"),
    [
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
    ],
)
def test_a_select_comes_back_from_its_store_then_its_state_then_the_default(
    restored, stored, current
):
    s, _ = _mode(restored=restored, stored=stored)
    _run(s.async_added_to_hass())
    assert s._attr_current_option == current
    assert s.entity_id == "select.nimbus_household_mode"


def test_choosing_stores_it_and_an_unknown_choice_is_refused():
    s, shared = _mode()
    _run(s.async_select_option(OTHER))
    assert (
        s.writes == [OTHER]
        and shared.store.data[const.CONF_HOUSEHOLD_MODE]["value"] == OTHER
    )
    with pytest.raises(ValueError, match="not a valid Nimbus"):
        _run(s.async_select_option("party"))
    assert s._attr_current_option == OTHER


def test_a_corrupt_select_store_reads_as_empty():
    shared = sel._SharedSelectStore(store=MemoryStore(broken=True))
    assert _run(shared.async_read("k")) is None
    _run(shared.async_write("k", "x"))
    assert shared.store.data["k"]["value"] == "x"


def test_setup_adds_the_four_selects(monkeypatch):
    added = []
    monkeypatch.setattr(
        sel,
        "async_get_integration",
        lambda hass, d: _value(SimpleNamespace(version="1")),
    )
    monkeypatch.setattr(sel, "Store", lambda *a: MemoryStore())
    monkeypatch.setattr(sel.pricing_source, "detected_profiles", lambda hass: {})
    monkeypatch.setattr(
        sel.pricing_source,
        "active_label",
        lambda options, profiles: sel.pricing_source.MANUAL,
    )
    monkeypatch.setattr(sel.pricing_source, "status", lambda options, profiles: {})
    _run(
        sel.async_setup_entry(
            None, SimpleNamespace(entry_id="hub1", options={}), added.extend
        )
    )
    assert [e.entity_id for e in added] == [
        "select.nimbus_household_mode",
        "select.nimbus_solver_load_forecast_source_policy",
        "select.nimbus_pricing_source",
        "select.nimbus_device_setup_mode",
    ]


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


def test_the_pricing_source_offers_what_is_detected_plus_manual(pricing):
    s, world, _ = pricing
    assert s._attr_options == ["Amber", sel.pricing_source.MANUAL]
    assert s._attr_current_option == sel.pricing_source.MANUAL
    world["profiles"]["Localvolts"] = {"id": "lv"}
    s._redetect()
    assert s._attr_options == ["Amber", "Localvolts", sel.pricing_source.MANUAL]
    assert s._attr_extra_state_attributes == {"profiles": ["Amber", "Localvolts"]}
    assert len(s.writes) == 1


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


def test_the_pricing_source_redetects_at_start_and_every_five_minutes(
    pricing, monkeypatch
):
    s, _, _ = pricing
    hooks = []
    monkeypatch.setattr(
        sel,
        "async_at_started",
        lambda hass, cb: hooks.append("started") or (lambda: None),
    )
    monkeypatch.setattr(
        sel,
        "async_track_time_interval",
        lambda hass, cb, every: hooks.append(every) or (lambda: None),
    )
    _run(s.async_added_to_hass())
    assert hooks == ["started", sel.timedelta(minutes=5)]


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


def test_the_setup_mode_shows_the_dry_run_and_open_drift_repairs(
    setup_mode, monkeypatch
):
    s, world, dc = setup_mode
    prefix = f"{dc.ISSUE_DRIFT}_hub1_"
    world["issues"] = {
        (const.DOMAIN, prefix + "a"): 1,
        (const.DOMAIN, prefix + "b"): 1,
        ("other", prefix + "c"): 1,
        (const.DOMAIN, "x"): 1,
    }
    ticks = []
    monkeypatch.setattr(
        sel,
        "async_track_time_interval",
        lambda hass, cb, every: ticks.append(every) or (lambda: None),
    )
    _run(s.async_added_to_hass())
    assert s._attr_current_option == "legacy" and s.writes == ["legacy"]
    assert s._attr_extra_state_attributes == {
        "revision": 3,
        "dry_run_clean": False,
        "dry_run_changes": 2,
        "dry_run_conflicts": 1,
        "dry_run_problems": 0,
        "devices": 3,
        "drift_repairs": 2,
    }
    assert ticks == [sel.timedelta(minutes=5)]


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
