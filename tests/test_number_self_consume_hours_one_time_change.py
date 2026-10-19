"""Self-consume after midnight, a one-time 4 -> 0 on upgrade (household,
19 Oct 2026; promised on #1700).

4 was the reference household's value, shipped as the default; no install
chose it, and a stored 4 cannot be told apart from the default. The upgrade
changes a stored 4 to 0 once, then writes a marker, so a household that sets
4 again keeps it. Every other value, and every other setting, is untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.helpers.storage import Store

from custom_components.nimbus_load.const import (
    CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
    DEFAULT_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
)
from custom_components.nimbus_load.number import (
    _DESCRIPTIONS,
    _ONE_TIME_VALUE_CHANGES,
    NimbusSolverNumber,
    _SharedNumberStore,
)

KEY = CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS
MARKER = _ONE_TIME_VALUE_CHANGES[KEY][0]
DESC = next(d for d in _DESCRIPTIONS if d.key == KEY)


def _entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {}
    return entry


def _start(store: _SharedNumberStore, restored: float | None, desc=DESC):
    """One HA start: the entity restores `restored` (None: nothing to
    restore) and runs its startup."""
    entity = NimbusSolverNumber(_entry(), desc, sw_version=None, shared_store=store)
    entity.async_get_last_number_data = AsyncMock(
        return_value=None if restored is None else MagicMock(native_value=restored)
    )
    state = MagicMock()
    state.last_updated.timestamp.return_value = 0.0
    entity.async_get_last_state = AsyncMock(return_value=state)
    asyncio.run(entity.async_added_to_hass())
    return entity


def _store(name: str) -> _SharedNumberStore:
    return _SharedNumberStore(store=Store(MagicMock(), 1, name))


def test_the_default_is_now_zero():
    assert DEFAULT_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS == 0
    assert DESC.default == 0


def test_a_stored_four_becomes_zero_once():
    store = _store("sc-four")
    entity = _start(store, 4.0)
    assert entity._attr_native_value == 0.0
    assert asyncio.run(store.async_read(KEY)) == 0.0
    assert asyncio.run(store.async_read(MARKER)) == 1.0


def _set_then_restart(store: _SharedNumberStore, value: float):
    """The household sets `value` (written through to the store, as
    `async_set_native_value` does), then HA restarts and restores it."""
    asyncio.run(store.async_write(KEY, value))
    return _start(store, value)


def test_four_set_again_after_the_change_is_kept():
    store = _store("sc-again")
    _start(store, 4.0)
    assert _set_then_restart(store, 4.0)._attr_native_value == 4.0


def test_any_other_value_is_left_alone_and_never_changed_later():
    for value in (0.0, 2.0, 6.0):
        store = _store(f"sc-{value}")
        entity = _start(store, value)
        assert entity._attr_native_value == value
        assert asyncio.run(store.async_read(MARKER)) == 1.0
        # Marked: a 4 set later is a choice, kept.
        assert _set_then_restart(store, 4.0)._attr_native_value == 4.0


def test_a_fresh_install_starts_at_zero():
    entity = _start(_store("sc-fresh"), None)
    assert entity._attr_native_value == 0


def test_no_other_setting_is_touched():
    other = next(
        d for d in _DESCRIPTIONS if d.key != KEY and d.max_value >= 4 >= d.min_value
    )
    store = _store("sc-other")
    entity = _start(store, 4.0, desc=other)
    assert entity._attr_native_value == 4.0
    assert asyncio.run(store.async_read(MARKER)) is None
