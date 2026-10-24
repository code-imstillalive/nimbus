"""The Solver's switches and time-of-day settings: where each comes back
from after a restart, what a change sets off, and the times' move from
numbers (#1837).

Covers what lived in #85, the solver time settings (household, 11 Oct
2026), the Solcast first default, and the switch and time files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
# The shared stubs have no time platform (the old suite only read time.py's
# text); a bare base class is all NimbusSolverTime needs.
if "homeassistant.components.time" not in sys.modules:
    _time_platform = types.ModuleType("homeassistant.components.time")
    _time_platform.TimeEntity = type("TimeEntity", (), {})
    sys.modules["homeassistant.components.time"] = _time_platform
_device_registry = sys.modules["homeassistant.helpers.device_registry"]
if not hasattr(_device_registry, "DeviceInfo"):
    _device_registry.DeviceInfo = dict  # a TypedDict in Home Assistant

swm = importlib.import_module("custom_components.nimbus_load.switch")
tm = importlib.import_module("custom_components.nimbus_load.time")
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


def _last(state, at=100.0):
    return SimpleNamespace(state=state, last_updated=datetime.fromtimestamp(at, UTC))


# --- switches ----------------------------------------------------------------------


def _switch(
    key=const.CONF_SOLVER_DISPATCH_DRY_RUN,
    *,
    default=False,
    last=None,
    store=None,
    options=None,
    hass=None,
):
    shared = swm._SharedSwitchStore(store=MemoryStore(store))
    entry = SimpleNamespace(entry_id="hub1", options=options or {})
    s = swm.NimbusSolverSwitch(entry, key, "Name", default, "0.94.485", shared)
    s.async_get_last_state = lambda: _value(last)
    s.writes = []
    s.async_write_ha_state = lambda: s.writes.append(s._attr_is_on)
    s.hass = hass
    return s, shared


def test_setup_adds_every_solver_switch_once(monkeypatch):
    added = []
    monkeypatch.setattr(
        swm,
        "async_get_integration",
        lambda hass, d: _value(SimpleNamespace(version=None)),
    )
    monkeypatch.setattr(swm, "Store", lambda *a: MemoryStore())
    _run(
        swm.async_setup_entry(
            None, SimpleNamespace(entry_id="hub1", options={}), added.extend
        )
    )
    ids = [s.entity_id for s in added]
    assert len(ids) == len(set(ids)) == 12
    assert "switch.nimbus_solver_dispatch_dry_run" in ids
    assert all(s._attr_device_info["sw_version"] is None for s in added)


def test_the_restored_state_wins_unless_the_store_is_newer():
    s, shared = _switch(
        last=_last("on", 100.0),
        store={
            const.CONF_SOLVER_DISPATCH_DRY_RUN: {"value": False, "written_at": 50.0}
        },
    )
    _run(s.async_added_to_hass())
    assert (
        s._attr_is_on is True
        and shared.store.data[const.CONF_SOLVER_DISPATCH_DRY_RUN]["value"] is True
    )
    newer, _ = _switch(
        last=_last("on", 100.0),
        store={
            const.CONF_SOLVER_DISPATCH_DRY_RUN: {"value": False, "written_at": 200.0}
        },
    )
    _run(newer.async_added_to_hass())
    assert newer._attr_is_on is False


@pytest.mark.parametrize(
    ("last", "store", "options", "on"),
    [
        (
            _last("unavailable"),
            {const.CONF_SOLVER_DISPATCH_DRY_RUN: {"value": True, "written_at": 1}},
            {},
            True,
        ),
        (
            None,
            {const.CONF_SOLVER_DISPATCH_DRY_RUN: {"value": "x"}},
            {const.CONF_SOLVER_DISPATCH_DRY_RUN: True},
            True,
        ),
        (
            None,
            None,
            {const.CONF_SOLVER_DISPATCH_DRY_RUN: "yes"},
            False,
        ),  # only a real bool seeds
        (None, None, {}, False),
    ],
)
def test_without_a_restored_state_the_store_then_the_options_seed_it(
    last, store, options, on
):
    s, _ = _switch(last=last, store=store, options=options)
    _run(s.async_added_to_hass())
    assert s._attr_is_on is on


@pytest.mark.parametrize(
    ("solcast", "on"),
    [(["sensor.solcast_today"], True), ([], False), (RuntimeError, False)],
)
def test_auto_include_starts_on_when_solcast_is_installed(monkeypatch, solcast, on):
    def entities(hass):
        if solcast is RuntimeError:
            raise RuntimeError("registry")
        return solcast

    monkeypatch.setattr(swm.known_sources, "solcast_entities", entities)
    s, shared = _switch(
        const.CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
        default=False,
        hass=SimpleNamespace(),
    )
    _run(s.async_added_to_hass())
    assert s._attr_is_on is on
    assert (shared.store.data is not None) is on  # only a changed first value is stored


def test_switching_stores_the_value_and_solve_on_price_change_rewires_the_watcher(
    monkeypatch,
):
    pkg = importlib.import_module("custom_components.nimbus_load")
    rewired = []
    monkeypatch.setattr(
        pkg,
        "_configure_price_watcher",
        lambda hass, entry: rewired.append(entry.entry_id),
    )
    s, shared = _switch()
    _run(s.async_turn_on())
    _run(s.async_turn_off())
    assert s.writes == [True, False] and shared.store.data[s._key]["value"] is False
    assert rewired == []
    w, _ = _switch(const.CONF_SOLVE_ON_PRICE_CHANGE)
    _run(w.async_turn_on())
    assert rewired == ["hub1"]


def test_a_corrupt_switch_store_reads_as_empty():
    shared = swm._SharedSwitchStore(store=MemoryStore(broken=True))
    assert _run(shared._async_read_entry("k")) is None
    _run(shared.async_write("k", True))
    assert shared.store.data == {
        "k": {"value": True, "written_at": shared.store.data["k"]["written_at"]}
    }


# --- times of day -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "clock"),
    [
        (0.0, time(0, 0)),
        (5.5, time(5, 30)),
        (17.25, time(17, 15)),
        (24.0, time(0, 0)),
        (23.999, time(0, 0)),
    ],
)
def test_hours_and_clock_times_convert_both_ways(hours, clock):
    assert tm.hours_to_time(hours) == clock
    # Round to the minute, wrap at midnight, and back.
    assert tm.time_to_hours(clock) == (round(hours * 60) % 1440) / 60


@pytest.mark.parametrize(
    ("state", "hours"),
    [("05:30:00", 5.5), ("17:15", 17.25), ("unknown", None), (None, None)],
)
def test_a_time_entitys_state_reads_as_hours(state, hours):
    assert tm.state_to_hours(state) == hours


def _time(key=None, *, last=None, own=None, number=None, options=None):
    key = key or tm.TIME_SETTINGS[0][0]
    t = tm.NimbusSolverTime(
        SimpleNamespace(entry_id="hub1", options=options or {}),
        key,
        "Name",
        "0.94.485",
        MemoryStore(own),
        MemoryStore(number),
    )
    t.async_get_last_state = lambda: _value(last)
    t.writes = []
    t.async_write_ha_state = lambda: t.writes.append(t._attr_native_value)
    return t, key


def test_a_time_comes_back_from_its_state_then_its_store_then_the_old_number():
    t, key = _time(last=SimpleNamespace(state="02:45:00"))
    _run(t.async_added_to_hass())
    assert t._attr_native_value == time(2, 45)
    own, key = _time(own={tm.TIME_SETTINGS[0][0]: {"value": 6.5}})
    _run(own.async_added_to_hass())
    assert own._attr_native_value == time(6, 30)
    # Upgrading from the number entity: its stored hours carry over and are kept.
    moved, key = _time(number={tm.TIME_SETTINGS[0][0]: 22.0})
    _run(moved.async_added_to_hass())
    assert moved._attr_native_value == time(22, 0)
    assert moved._own_store.data[key]["value"] == 22.0


def test_without_any_saved_value_the_options_then_the_default_apply():
    key = tm.TIME_SETTINGS[0][0]
    t, _ = _time(options={key: "3.25"})
    _run(t.async_added_to_hass())
    assert t._attr_native_value == time(3, 15)
    bad, _ = _time(options={key: "soon"}, own={key: {"value": "x"}})
    _run(bad.async_added_to_hass())
    assert bad._attr_native_value == tm.hours_to_time(tm.TIME_DEFAULTS.get(key, 0.0))
    broken, _ = _time()
    broken._own_store.broken = broken._number_store.broken = True
    _run(broken.async_added_to_hass())
    assert broken._attr_native_value == tm.hours_to_time(tm.TIME_DEFAULTS.get(key, 0.0))


def test_setting_a_time_stores_its_hours():
    t, key = _time()
    _run(t.async_set_value(time(5, 30)))
    assert t.writes == [time(5, 30)] and t._own_store.data[key]["value"] == 5.5


def test_setup_replaces_the_old_number_entities_with_times(monkeypatch):
    removed, added = [], []
    old_ids = {f"hub1_{key}": f"number.nimbus_{key}" for key, _ in tm.TIME_SETTINGS[:2]}
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, uid: old_ids.get(uid),
        async_remove=removed.append,
    )
    monkeypatch.setattr(tm.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        tm,
        "async_get_integration",
        lambda hass, d: _value(SimpleNamespace(version="1")),
    )
    monkeypatch.setattr(tm, "Store", lambda *a: MemoryStore())
    _run(
        tm.async_setup_entry(
            None, SimpleNamespace(entry_id="hub1", options={}), added.extend
        )
    )
    assert removed == list(old_ids.values())
    assert [t.entity_id for t in added] == [
        f"time.nimbus_{k}" for k, _ in tm.TIME_SETTINGS
    ]
    p2p = [t for t in added if t._key.startswith("solver_p2p_")]
    assert p2p and all(t._attr_device_info["name"] == "Nimbus P2P" for t in p2p)


def test_the_price_event_arm_is_its_own_switch_and_starts_off(monkeypatch):
    """#1213: a simulated price event must never be something an install
    drifts into, so its arm is a separate switch that starts off."""
    added = []
    monkeypatch.setattr(
        swm,
        "async_get_integration",
        lambda hass, d: _value(SimpleNamespace(version=None)),
    )
    monkeypatch.setattr(swm, "Store", lambda *a: MemoryStore())
    _run(
        swm.async_setup_entry(
            None, SimpleNamespace(entry_id="hub1", options={}), added.extend
        )
    )
    arm = next(
        s for s in added if s.entity_id == "switch.nimbus_solver_price_event_enabled"
    )
    assert arm._attr_name == "Price Event Simulation Armed"
    assert arm._attr_is_on is False


def test_the_p2p_block_times_are_times_and_only_times(monkeypatch):
    """#565: each P2P block's start and end is a time entity on the P2P
    sub-device, never also a number, and the solver config reads all six as
    times."""
    monkeypatch.setattr(
        tm.er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get_entity_id=lambda *a: None, async_remove=lambda e: None
        ),
    )
    monkeypatch.setattr(
        tm,
        "async_get_integration",
        lambda hass, d: _value(SimpleNamespace(version="1")),
    )
    monkeypatch.setattr(tm, "Store", lambda *a: MemoryStore())
    added = []
    _run(
        tm.async_setup_entry(
            None, SimpleNamespace(entry_id="hub1", options={}), added.extend
        )
    )
    blocks = {
        getattr(const, f"CONF_SOLVER_P2P_BLOCK_{n}_{end}_HOUR"): f"P2P Block {n} {word}"
        for n in (1, 2, 3)
        for end, word in (("START", "From"), ("END", "Until"))
    }
    named = {t._key: t for t in added if t._key in blocks}
    assert {k: t._attr_name for k, t in named.items()} == blocks
    assert all(t._attr_device_info["name"] == "Nimbus P2P" for t in named.values())
    number = importlib.import_module("custom_components.nimbus_load.number")
    assert not set(blocks) & {d.key for d in number._DESCRIPTIONS}
    sensor = importlib.import_module("custom_components.nimbus_load.sensor")
    assert set(blocks) <= set(sensor._SOLVER_TIME_ENTITY_KEYS)
