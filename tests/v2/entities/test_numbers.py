"""The Solver's and each controllable load's number settings: their table,
where a value comes back from after a restart, and what a change sets off
(#1837).

Covers what lived in #85 (restore race), #1013, the shared number store,
the one-time post-window value change, currency units, and the number files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

nm = importlib.import_module("custom_components.nimbus_load.number")
sm = importlib.import_module("custom_components.nimbus_load.sensor")
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


def _run(coro):
    return asyncio.run(coro)


# --- the table ---------------------------------------------------------------------


def test_every_solver_setting_starts_inside_its_own_range():
    for desc in nm._DESCRIPTIONS:
        assert desc.min_value <= desc.default <= desc.max_value
        assert desc.step > 0


def test_every_load_setting_starts_inside_its_own_range():
    for desc in nm._CONTROLLABLE_LOAD_DESCRIPTIONS:
        assert desc.min_value <= desc.default <= desc.max_value
        assert desc.kinds


def test_every_number_the_solver_reads_has_an_entity_and_ids_are_unique():
    keys = [d.key for d in nm._DESCRIPTIONS]
    assert len(keys) == len(set(keys))
    assert set(sm._SOLVER_NUMBER_ENTITY_KEYS) <= set(keys)


# --- the shared store ----------------------------------------------------------------


def test_a_stored_value_reads_back_with_when_it_was_written():
    for data, read in [
        ({"k": {"value": 2.5, "written_at": 10.0}}, (2.5, 10.0)),
        ({"k": 3}, (3.0, 0.0)),  # written before timestamps existed
        ({"k": {"value": "x", "written_at": 1}}, None),
        ({"k": {"written_at": 1}}, None),
        ({}, None),
        (None, None),
    ]:
        store = nm._SharedNumberStore(store=MemoryStore(data))
        assert _run(store._async_read_entry("k")) == read
        assert _run(store.async_read("k")) == (read[0] if read else None)


def test_a_corrupt_store_reads_as_empty_and_is_rewritten():
    store = nm._SharedNumberStore(store=MemoryStore(broken=True))
    assert _run(store.async_read("k")) is None
    store.store.broken = False
    _run(store.async_write("k", 4.0))
    assert store.store.data["k"]["value"] == 4.0


# --- coming back after a restart ------------------------------------------------------


def _desc(key=None):
    return next(
        d for d in nm._DESCRIPTIONS if d.key == (key or nm._DESCRIPTIONS[0].key)
    )


def _number(
    *, restored=None, restored_at=None, store=None, options=None, key=None, hass=True
):
    store = nm._SharedNumberStore(store=MemoryStore(store))
    entry = SimpleNamespace(entry_id="hub1", options=options or {})
    n = nm.NimbusSolverNumber(entry, _desc(key), "0.94.485", store)
    writes, tasks = [], []

    async def last_data():
        return None if restored is None else SimpleNamespace(native_value=restored)

    async def last_state():
        if restored_at is None:
            return None
        return SimpleNamespace(last_updated=datetime.fromtimestamp(restored_at, UTC))

    n.async_get_last_number_data = last_data
    n.async_get_last_state = last_state
    n.async_write_ha_state = lambda: writes.append(n._attr_native_value)
    if hass:
        n.hass = SimpleNamespace(
            config=SimpleNamespace(currency="AUD"),
            async_create_task=lambda coro: tasks.append(coro) or coro.close(),
        )
    return n, store, writes, tasks


def test_the_value_restored_by_home_assistant_wins_unless_the_store_is_newer():
    key = _desc().key
    n, store, _, _ = _number(
        restored=7.0, restored_at=100.0, store={key: {"value": 9.0, "written_at": 50.0}}
    )
    _run(n.async_added_to_hass())
    assert n._attr_native_value == 7.0
    assert store.store.data[key]["value"] == 7.0  # copied back to the store
    newer, _, _, _ = _number(
        restored=7.0,
        restored_at=100.0,
        store={key: {"value": 9.0, "written_at": 200.0}},
    )
    _run(newer.async_added_to_hass())
    assert newer._attr_native_value == 9.0


def test_without_a_restored_value_the_store_then_the_options_seed_it():
    key = _desc().key
    from_store, _, _, _ = _number(store={key: 3})
    _run(from_store.async_added_to_hass())
    assert from_store._attr_native_value == 3.0
    seeded, store, _, _ = _number(options={key: "4.5"})
    _run(seeded.async_added_to_hass())
    assert seeded._attr_native_value == 4.5 and store.store.data[key]["value"] == 4.5
    bad, _, _, _ = _number(options={key: "lots"})
    _run(bad.async_added_to_hass())
    assert bad._attr_native_value == _desc().default


def test_the_one_time_post_window_change_runs_once_and_only_from_the_old_value():
    key = const.CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS
    marker, old, new = nm._ONE_TIME_VALUE_CHANGES[key]
    upgraded, store, _, _ = _number(key=key, store={key: old})
    _run(upgraded.async_added_to_hass())
    assert upgraded._attr_native_value == new
    assert marker in store.store.data
    # The household sets it back: the next start leaves it.
    again, _, _, _ = _number(
        key=key, store={**store.store.data, key: {"value": old, "written_at": 9e9}}
    )
    _run(again.async_added_to_hass())
    assert again._attr_native_value == old
    other, store2, _, _ = _number(key=key, store={key: 2.0})
    _run(other.async_added_to_hass())
    assert other._attr_native_value == 2.0 and marker in store2.store.data


def test_a_change_is_stored_and_tells_the_device_contract(monkeypatch):
    n, store, writes, tasks = _number()
    dc = importlib.import_module("custom_components.nimbus_load.device_contract_ha")
    seen = []

    async def changed(hass, entry, key, value):
        seen.append((key, value))

    monkeypatch.setattr(dc, "async_number_changed", changed)
    _run(n.async_set_native_value(6.0))
    assert writes == [6.0] and store.store.data[_desc().key]["value"] == 6.0
    assert len(tasks) == 1


def test_the_debounce_change_reconfigures_the_price_watcher(monkeypatch):
    pkg = importlib.import_module("custom_components.nimbus_load")
    calls = []
    monkeypatch.setattr(
        pkg,
        "_configure_price_watcher",
        lambda hass, entry: calls.append(entry.entry_id),
    )
    n, _, _, _ = _number(key=const.CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S)
    _run(n.async_set_native_value(30.0))
    assert calls == ["hub1"]


def test_a_money_setting_is_shown_in_the_households_currency():
    for unit, currency, shown in [
        (nm._CURRENCY_PER_KWH, "AUD", "AUD/kWh"),
        (nm._CURRENCY_PER_DAY, "AUD", "AUD/day"),
        (nm._CURRENCY, "AUD", "AUD"),
        (nm._CURRENCY_PER_KWH, "", None),
        ("kW", "AUD", "kW"),
    ]:
        n, _, _, _ = _number()
        n._desc = SimpleNamespace(unit=unit)
        n.hass.config.currency = currency
        assert n.native_unit_of_measurement == shown
        n.hass = None
        assert n.native_unit_of_measurement == unit


# --- per-load settings and setup ------------------------------------------------------


def _load_sub(kind):
    return SimpleNamespace(
        subentry_id="load1",
        subentry_type=const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        title="Hot Water",
        data={const.CONF_CONTROLLABLE_LOAD_KIND: kind},
    )


def test_setup_adds_every_solver_setting_and_each_loads_own_for_its_kind(monkeypatch):
    added = []
    monkeypatch.setattr(
        nm,
        "async_get_integration",
        lambda hass, domain: _done(SimpleNamespace(version="0.94.485")),
    )
    monkeypatch.setattr(
        nm.sensor_flattened, "resolve_hub_device_id", lambda hass, entry: None
    )
    monkeypatch.setattr(nm, "Store", lambda hass, version, key: MemoryStore())
    kind = nm._CONTROLLABLE_LOAD_DESCRIPTIONS[0].kinds[0]
    entry = SimpleNamespace(
        entry_id="hub1",
        options={},
        subentries={
            "load1": _load_sub(kind),
            "other": SimpleNamespace(subentry_type="load"),
        },
    )
    _run(nm.async_setup_entry(None, entry, lambda ents, **kw: added.append((ents, kw))))
    hub, *loads = added
    assert len(hub[0]) == len(nm._DESCRIPTIONS)
    assert len(loads) == 1 and loads[0][1] == {"config_subentry_id": "load1"}
    expected = [d for d in nm._CONTROLLABLE_LOAD_DESCRIPTIONS if kind in d.kinds]
    assert [e._desc.key for e in loads[0][0]] == [d.key for d in expected]
    first = loads[0][0][0]
    assert first.entity_id == f"number.nimbus_hot_water_{expected[0].key}"
    assert first._attr_unique_id == f"load1_{expected[0].key}"


async def _async(value):
    return value


def _done(value):
    return _async(value)


def test_a_loads_setting_restores_and_stores_under_its_own_key():
    store = nm._SharedNumberStore(store=MemoryStore({}))
    desc = nm._CONTROLLABLE_LOAD_DESCRIPTIONS[0]
    sub = _load_sub(desc.kinds[0])
    sub.data[desc.key] = desc.max_value
    entry = SimpleNamespace(entry_id="hub1", options={})
    n = nm.NimbusControllableLoadNumber(entry, sub, desc, "0.94.485", store)
    n.async_get_last_number_data = lambda: _async(None)
    n.async_get_last_state = lambda: _async(None)
    written = []
    n.async_write_ha_state = lambda: written.append(n._attr_native_value)
    _run(n.async_added_to_hass())
    assert n._attr_native_value == desc.max_value  # seeded from the load's wizard value
    _run(n.async_set_native_value(desc.min_value))
    assert store.store.data[f"load1_{desc.key}"]["value"] == desc.min_value
    assert written == [desc.min_value]
