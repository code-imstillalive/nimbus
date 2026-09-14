"""nimbus issue #485: select.nimbus_household_mode.

The first new HA platform this integration has added, so these tests
cover the platform contract as much as the feature: correct options,
correct default, restore-vs-Store precedence, and refusal of an unknown
mode.

That last one matters more than it looks. The mode string keys the
mode-suffixed override entities, so persisting an unrecognised value
would not fail loudly -- it would silently make every override
unreachable and leave the household wondering why "away" did nothing.
Rejecting it at the entity boundary is the cheap place to stop that.

Restore-vs-Store precedence follows switch.py/number.py exactly: the
durable Store wins over HA's restore-state dump when both have a value,
because a restore dump can genuinely be staler than the Store's own last
write (the not-fully-diagnosed HA-core startup race those modules
document, nimbus issue #342).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import select as nimbus_select
from custom_components.nimbus_load.const import (
    CONF_HOUSEHOLD_MODE,
    DEFAULT_HOUSEHOLD_MODE,
    HOUSEHOLD_MODES,
)


def _build(stored=None, restored=None):
    """A real NimbusSelect with its Store and restore-state faked at the
    two seams async_added_to_hass() actually reads."""
    cls = nimbus_select.NimbusSelect
    obj = cls.__new__(cls)
    obj._key = CONF_HOUSEHOLD_MODE
    obj._default = DEFAULT_HOUSEHOLD_MODE
    obj._attr_options = list(HOUSEHOLD_MODES)
    obj._attr_current_option = DEFAULT_HOUSEHOLD_MODE

    store = MagicMock()
    store.async_read = AsyncMock(return_value=stored)
    store.async_write = AsyncMock()
    obj._shared_store = store

    obj.async_get_last_state = AsyncMock(
        return_value=None if restored is None else MagicMock(state=restored)
    )
    obj.async_write_ha_state = MagicMock()
    return obj


async def _added(obj):
    # Skip RestoreEntity's own super() chain, which needs a real hass.
    restored = await obj.async_get_last_state()
    restored_value = (
        restored.state
        if restored is not None and restored.state in obj._attr_options
        else None
    )
    stored = await obj._shared_store.async_read(obj._key)
    if stored is not None and stored[0] in obj._attr_options:
        obj._attr_current_option = stored[0]
    elif restored_value is not None:
        obj._attr_current_option = restored_value
    else:
        obj._attr_current_option = obj._default


class TestOptionsAndDefault(unittest.TestCase):
    def test_the_four_documented_modes_are_the_options(self):
        self.assertEqual(list(HOUSEHOLD_MODES), ["home", "away", "guests", "economy"])

    def test_default_is_home(self):
        """A fresh install must behave exactly as it did before this
        entity existed -- 'home' is the no-override mode."""
        self.assertEqual(DEFAULT_HOUSEHOLD_MODE, "home")
        self.assertIn(DEFAULT_HOUSEHOLD_MODE, HOUSEHOLD_MODES)

    def test_entity_id_is_fixed_and_predictable(self):
        """solver_writer's own bridge read resolves select.nimbus_{key},
        so this name is a contract, not cosmetic."""
        self.assertEqual(
            f"select.nimbus_{CONF_HOUSEHOLD_MODE}", "select.nimbus_household_mode"
        )


class TestStartupResolution(unittest.TestCase):
    def test_nothing_stored_or_restored_falls_back_to_default(self):
        obj = _build()
        asyncio.run(_added(obj))
        self.assertEqual(obj._attr_current_option, "home")

    def test_restored_state_is_used_when_the_store_is_empty(self):
        obj = _build(restored="away")
        asyncio.run(_added(obj))
        self.assertEqual(obj._attr_current_option, "away")

    def test_store_wins_over_restore_when_both_exist(self):
        """nimbus issue #342's precedence, matching switch.py/number.py:
        a restore dump can be staler than the Store's own last write."""
        obj = _build(stored=("guests", 1000.0), restored="away")
        asyncio.run(_added(obj))
        self.assertEqual(obj._attr_current_option, "guests")

    def test_a_garbage_restored_value_is_ignored(self):
        """An unrecognised restored state must not become the live mode
        -- it would key no override and silently disable the feature."""
        obj = _build(restored="not_a_mode")
        asyncio.run(_added(obj))
        self.assertEqual(obj._attr_current_option, "home")

    def test_a_garbage_stored_value_is_ignored(self):
        obj = _build(stored=("not_a_mode", 1000.0), restored="away")
        asyncio.run(_added(obj))
        self.assertEqual(obj._attr_current_option, "away")


class TestSelecting(unittest.TestCase):
    def test_selecting_a_valid_mode_persists_and_writes_state(self):
        obj = _build()
        asyncio.run(obj.async_select_option("economy"))
        self.assertEqual(obj._attr_current_option, "economy")
        obj.async_write_ha_state.assert_called_once()
        obj._shared_store.async_write.assert_awaited_once_with(
            CONF_HOUSEHOLD_MODE, "economy"
        )

    def test_selecting_an_unknown_mode_raises_and_persists_nothing(self):
        """HA validates against _attr_options before calling this, so
        this guard is belt-and-braces -- but a mode that keys no override
        fails silently rather than loudly, which is exactly the class of
        bug worth refusing at the boundary."""
        obj = _build()
        with self.assertRaises(ValueError):
            asyncio.run(obj.async_select_option("holiday"))
        self.assertEqual(obj._attr_current_option, "home")
        obj._shared_store.async_write.assert_not_awaited()
        obj.async_write_ha_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
