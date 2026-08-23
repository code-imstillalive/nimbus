"""Real test of NimbusSolverSwitch (switch.py) -- entity-attribute wiring
plus its 3-branch async_added_to_hass() restore logic (restored state wins,
then a real bool seeded in entry.options, then the field's own default).

Imports and exercises the REAL class (not a reimplementation) against mock
hass/entry objects, via tests/_ha_stubs.py's stand-in homeassistant.*
modules -- the real `homeassistant` package isn't installed in this
project's local dev environment.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import DOMAIN  # noqa: E402
from custom_components.nimbus_load.switch import NimbusSolverSwitch  # noqa: E402


def _make_entity(key="auto_include_known_solar", default=False, options=None):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = options or {}
    return NimbusSolverSwitch(
        entry, key, "Auto Include Known Solar", default, sw_version="9.9.9-test"
    )


def test_entity_attribute_wiring():
    entity = _make_entity(default=True)
    assert entity._attr_unique_id == "test_entry_id_auto_include_known_solar"
    assert entity.entity_id == "switch.nimbus_auto_include_known_solar"
    assert entity._attr_name == "Auto Include Known Solar"
    assert entity._attr_is_on is True
    assert (DOMAIN, "test_entry_id") in entity._attr_device_info["identifiers"]


def test_restored_state_on_wins_over_everything():
    entity = _make_entity(default=False, options={"auto_include_known_solar": False})
    entity.async_get_last_state = AsyncMock(return_value=MagicMock(state="on"))
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_on is True


def test_restored_state_off_wins_over_everything():
    entity = _make_entity(default=True, options={"auto_include_known_solar": True})
    entity.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))
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
    entity.async_get_last_state = AsyncMock(return_value=MagicMock(state="unavailable"))
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
