"""Real test of NimbusSolverSwitch (switch.py) -- entity-attribute wiring,
its restore-state logic, and (nimbus issue #342, Mark Purcell) its own
durable Store backstop -- same "restore, then Store, then options-seed,
then default" fallback chain and freshness-vs-Store compare number.py's
own NimbusSolverNumber already has, this platform previously had none of
it beyond a bare restore-then-options-seed.

Imports and exercises the REAL class (not a reimplementation) against mock
hass/entry objects, via tests/_ha_stubs.py's stand-in homeassistant.*
modules -- the real `homeassistant` package isn't installed in this
project's local dev environment.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.helpers.storage import Store

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.switch import NimbusSolverSwitch, _SharedSwitchStore

_store_key_counter = itertools.count()


def _fresh_shared_store(key: str | None = None) -> _SharedSwitchStore:
    # _StubStore's own backing dict is keyed by this literal string and
    # shared process-wide (same file == same data, matching real Store
    # semantics) -- a unique key per call is required so tests that don't
    # care about sharing don't silently pollute each other. Only tests
    # that explicitly want a shared store pass a real, shared key.
    return _SharedSwitchStore(
        store=Store(MagicMock(), 1, key or f"test-{next(_store_key_counter)}")
    )


def _last_state(state: str, timestamp: float = 1000.0) -> MagicMock:
    m = MagicMock()
    m.state = state
    m.last_updated.timestamp.return_value = timestamp
    return m


def _make_entity(
    key="auto_include_known_solar", default=False, options=None, shared_store=None
):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = options or {}
    return NimbusSolverSwitch(
        entry,
        key,
        "Auto Include Known Solar",
        default,
        sw_version="9.9.9-test",
        shared_store=shared_store or _fresh_shared_store(),
    )


def test_entity_attribute_wiring():
    entity = _make_entity(default=True)
    assert entity._attr_unique_id == "test_entry_id_auto_include_known_solar"
    assert entity.entity_id == "switch.nimbus_auto_include_known_solar"
    assert entity._attr_name == "Auto Include Known Solar"
    assert entity._attr_is_on is True
    assert (DOMAIN, "test_entry_id") in entity._attr_device_info["identifiers"]


def test_does_not_poll_it_has_no_async_update_at_all():
    # nimbus issue #365 (Mark Purcell): this class's state is driven
    # entirely by real user toggles and RestoreEntity, never by polling
    # anything external -- HA's default should_poll=True would call a
    # (nonexistent) update path on every one of these every 30s cycle
    # for no reason.
    assert NimbusSolverSwitch._attr_should_poll is False
    assert not hasattr(NimbusSolverSwitch, "async_update")


def test_restored_state_on_wins_over_everything():
    entity = _make_entity(default=False, options={"auto_include_known_solar": False})
    entity.async_get_last_state = AsyncMock(return_value=_last_state("on"))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True


def test_restored_state_off_wins_over_everything():
    entity = _make_entity(default=True, options={"auto_include_known_solar": True})
    entity.async_get_last_state = AsyncMock(return_value=_last_state("off"))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is False


def test_no_restored_state_falls_back_to_seeded_options_bool():
    entity = _make_entity(default=False, options={"auto_include_known_solar": True})
    entity.async_get_last_state = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True


def test_no_restored_state_and_no_seeded_value_uses_default():
    entity = _make_entity(default=True, options={})
    entity.async_get_last_state = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True  # unchanged from __init__'s own default


def test_non_bool_seeded_value_is_ignored_not_coerced():
    # A real, if unlikely, corruption case -- entry.options holding
    # something that isn't a real bool for this key shouldn't silently
    # get truthy-coerced into True/False.
    entity = _make_entity(default=False, options={"auto_include_known_solar": "yes"})
    entity.async_get_last_state = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is False  # falls through to _default, untouched


def test_state_neither_on_nor_off_string_is_treated_as_no_restored_state():
    # e.g. "unavailable"/"unknown" -- real values HA can hand back here.
    entity = _make_entity(default=False, options={"auto_include_known_solar": True})
    entity.async_get_last_state = AsyncMock(return_value=_last_state("unavailable"))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True  # falls through to the seeded options value


def test_turn_on_sets_is_on_and_writes_state():
    entity = _make_entity(default=False)
    entity.async_write_ha_state = MagicMock()
    asyncio.run(entity.async_turn_on())
    assert entity._attr_is_on is True
    entity.async_write_ha_state.assert_called_once()


def test_turn_off_sets_is_on_and_writes_state():
    entity = _make_entity(default=True)
    entity.async_write_ha_state = MagicMock()
    asyncio.run(entity.async_turn_off())
    assert entity._attr_is_on is False
    entity.async_write_ha_state.assert_called_once()


def test_is_entity_category_config():
    """Gold entity-category (2026-08-23) -- same reasoning as
    NimbusSolverNumber: a Solver tuning toggle, not a primary reading."""
    from homeassistant.const import EntityCategory

    assert NimbusSolverSwitch._attr_entity_category == EntityCategory.CONFIG


# --- nimbus issue #342: durable Store backstop -----------------------------


def test_successful_restore_backfills_the_store():
    shared_store = _fresh_shared_store("backfill")
    entity = _make_entity(shared_store=shared_store)
    entity.async_write_ha_state = MagicMock()
    entity.async_get_last_state = AsyncMock(return_value=_last_state("on", 1000.0))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True
    assert asyncio.run(shared_store._async_read_entry(entity._key))[0] is True


def test_restore_miss_falls_back_to_the_store():
    shared_store = _fresh_shared_store("restore-miss")
    asyncio.run(shared_store.async_write("auto_include_known_solar", True))
    entity = _make_entity(shared_store=shared_store)
    entity.async_get_last_state = AsyncMock(return_value=None)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True


def test_restore_older_than_store_does_not_overwrite_the_newer_store_value():
    """The real bug: a toggle at 10:00, last restore-state dump at 09:55,
    HA killed at 10:05 -- restoring the stale 09:55 value must NOT
    overwrite the Store's own correct, newer value."""
    shared_store = _fresh_shared_store("store-newer-than-restore")
    asyncio.run(shared_store.async_write("auto_include_known_solar", True))
    stored_at = asyncio.run(shared_store._async_read_entry("auto_include_known_solar"))[
        1
    ]

    entity = _make_entity(shared_store=shared_store, default=False)
    entity.async_get_last_state = AsyncMock(
        return_value=_last_state("off", stored_at - 300)
    )
    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_is_on is True, (
        "the newer Store value must win over a genuinely staler restore"
    )
    assert (
        asyncio.run(shared_store._async_read_entry("auto_include_known_solar"))[0]
        is True
    )


def test_restore_newer_than_store_wins_and_backfills():
    shared_store = _fresh_shared_store("restore-newer-than-store")
    asyncio.run(shared_store.async_write("auto_include_known_solar", False))
    stored_at = asyncio.run(shared_store._async_read_entry("auto_include_known_solar"))[
        1
    ]

    entity = _make_entity(shared_store=shared_store, default=False)
    entity.async_get_last_state = AsyncMock(
        return_value=_last_state("on", stored_at + 300)
    )
    asyncio.run(entity.async_added_to_hass())

    assert entity._attr_is_on is True
    assert (
        asyncio.run(shared_store._async_read_entry("auto_include_known_solar"))[0]
        is True
    )


def test_turn_on_writes_through_to_the_store():
    shared_store = _fresh_shared_store("turn-on-writes-through")
    entity = _make_entity(shared_store=shared_store, default=False)
    entity.async_write_ha_state = MagicMock()
    asyncio.run(entity.async_turn_on())
    assert asyncio.run(shared_store._async_read_entry(entity._key))[0] is True


def test_turn_off_writes_through_to_the_store():
    shared_store = _fresh_shared_store("turn-off-writes-through")
    entity = _make_entity(shared_store=shared_store, default=True)
    entity.async_write_ha_state = MagicMock()
    asyncio.run(entity.async_turn_off())
    assert asyncio.run(shared_store._async_read_entry(entity._key))[0] is False


def test_store_is_genuinely_shared_across_sibling_entities():
    shared_store = _fresh_shared_store("shared-across-siblings")
    entity_a = _make_entity(
        key="dispatch_dry_run", shared_store=shared_store, default=False
    )
    entity_a.async_write_ha_state = MagicMock()
    asyncio.run(entity_a.async_turn_on())

    entity_b = _make_entity(
        key="solve_on_price_change", shared_store=shared_store, default=False
    )
    entity_b.async_get_last_state = AsyncMock(return_value=None)
    asyncio.run(entity_b.async_added_to_hass())
    # entity_b's own key was never written -- must not see entity_a's
    # value under its own key.
    assert entity_b._attr_is_on is False
    # But entity_a's key IS visible to a fresh read through the same
    # shared store, proving it's one shared file, not per-entity.
    assert asyncio.run(shared_store._async_read_entry("dispatch_dry_run"))[0] is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
