"""Real regression test for nimbus issue #344 (Mark Purcell): a failed
first refresh leaks every sibling coordinator's daily retrain listener;
each ConfigEntryNotReady retry leaks another set.

Root cause: __init__.py's own async_unload_entry() only ever cleans up
coordinators reachable via entry.runtime_data -- which is assigned AFTER
asyncio.gather() over every subentry's _setup_one() returns successfully.
If ANY subentry's async_config_entry_first_refresh() raises
ConfigEntryNotReady, that exception propagates out of gather() before
runtime_data is ever assigned, so async_unload_entry()'s own
`for coordinator in entry.runtime_data.values(): coordinator.
async_unload()` loop has nothing to iterate against -- every coordinator
already constructed earlier in that same gather batch (whose async_setup()
already registered a daily retrain listener) leaks its listener. HA's own
retry-with-backoff on ConfigEntryNotReady means every retry leaks another
full set.

Fix: NimbusCoordinator.async_setup() also registers its own (already
idempotent) async_unload() with entry.async_on_unload() -- HA's own
guarantee that fires on ANY teardown of this entry, including a setup
attempt that fails before ever reaching async_unload_entry() at all.
Deliberately registers the COORDINATOR'S OWN async_unload method, not the
raw `async_track_time_change` unsub callable directly -- on the NORMAL
successful-setup path, async_unload_entry() ALSO calls coordinator.
async_unload() explicitly, and registering the raw callable here too
would call the same underlying HA listener-removal twice, reproducing the
exact double-unsub crash issue #337 was fixed for.

Imports and exercises the REAL async_setup() (not a reimplementation),
same bare-coordinator-via-__new__() technique as
test_coordinator_setup_does_not_block_on_retrain.py.
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
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _make_bare_coordinator() -> NimbusCoordinator:
    return NimbusCoordinator.__new__(NimbusCoordinator)


def _setup_coordinator(*, trained) -> NimbusCoordinator:
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=trained)
    coord.hass.async_create_task = MagicMock()
    coord.entry = MagicMock()
    coord.entry.options = {}
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry"
    asyncio.run(coord.async_setup())
    return coord


def test_async_setup_registers_its_own_async_unload_with_entry_on_unload():
    coord = _setup_coordinator(trained="already-trained-sentinel")

    coord.entry.async_on_unload.assert_called_once_with(coord.async_unload)


def test_registered_safety_net_actually_unsubs_the_retrain_listener():
    """Not just "was it called with the right thing" -- prove the
    registered callable, when HA itself invokes it (e.g. on this failed
    setup attempt's own teardown), genuinely cancels the real listener."""
    coord = _setup_coordinator(trained="already-trained-sentinel")
    # Capture whatever async_on_unload was actually given, exactly as HA
    # core would hold onto it and call it later.
    registered_callback = coord.entry.async_on_unload.call_args[0][0]
    unsub_mock = MagicMock()
    coord._unsub_retrain = unsub_mock

    registered_callback()

    unsub_mock.assert_called_once()
    assert coord._unsub_retrain is None


def test_safety_net_is_idempotent_when_the_normal_unload_path_already_ran():
    """The real double-unsub risk this fix must not reintroduce (issue
    #337's own failure shape): on a NORMAL successful setup + later
    unload, async_unload_entry() calls coordinator.async_unload()
    explicitly BEFORE HA core processes the async_on_unload hooks. The
    registered callback IS coordinator.async_unload itself (not the raw
    unsub), so a second invocation must be a safe no-op, never a second
    real call into the underlying HA listener-removal."""
    coord = _setup_coordinator(trained="already-trained-sentinel")
    unsub_mock = MagicMock()
    coord._unsub_retrain = unsub_mock

    coord.async_unload()  # the "normal" explicit teardown call
    unsub_mock.assert_called_once()
    assert coord._unsub_retrain is None

    registered_callback = coord.entry.async_on_unload.call_args[0][0]
    registered_callback()  # HA core's own later on_unload processing

    # Still exactly one real call into the underlying unsub -- the second
    # invocation found _unsub_retrain already None and did nothing.
    unsub_mock.assert_called_once()


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
