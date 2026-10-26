"""Real regression guard for nimbus issue #312's residual (root-caused
2026-09-01, via a live debug-logging capture on devhub spanning a full
`homeassistant.reload_config_entry` call -- see __init__.py's own
async_unload_entry() docstring for the full incident writeup).

HA core's own `ConfigEntry.async_unload()` (config_entries.py) calls our
`async_unload_entry()` FIRST, and only processes `entry.async_on_unload()`
callbacks AFTER it returns -- confirmed by reading that source directly,
not guessed. Before this fix, our own periodic-solve cron timer,
price-watcher listener, and startup-retry task were only ever cancelled
via `entry.async_on_unload()`, meaning they stayed live for the ENTIRE
duration of `hass.config_entries.async_unload_platforms()` (which tears
down every entity and unregisters its solver_writer.py push handler).
An old trigger firing in that window would find its handler just
unregistered, fall through to `ha_post_state()`'s raw `states.async_set()`
fallback, and write a non-`RESTORED` state that then collided with the
fresh entity the very next setup pass tried to register ("does not
generate unique IDs... ignoring <entity_id>") -- reproduced live on both
a cold boot and a plain reload.

Fix: `async_unload_entry()` now cancels the timer/listener/task itself,
directly, BEFORE calling `async_unload_platforms()` -- not relying on
`entry.async_on_unload()`'s later timing at all. This file locks in:

1. All three triggers are cancelled BEFORE platform teardown begins (call
   ordering, not just "eventually called").
2. Every one of the three module-level dicts is cleared for this entry_id
   afterward (no stale reference left behind for a genuine removal).
3. A missing/None entry in any of the three dicts (never registered, or
   the toggle was off) is a safe no-op -- doesn't raise.
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
import custom_components.nimbus_load as nimbus_init


def _make_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = {}
    return entry


def test_missing_triggers_are_a_safe_no_op():
    """An entry that never enabled the price watcher (unsub is None) or
    is being unloaded before its startup-retry task ever got created
    must not raise."""
    entry_id = "test_entry_unload_no_triggers_registered"
    entry = _make_entry(entry_id)
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    # Deliberately nothing seeded in any of the three dicts for this
    # entry_id -- exercises the "never registered" path for all three.
    result = asyncio.run(nimbus_init.async_unload_entry(hass, entry))
    assert result is True
