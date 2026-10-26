"""Real test of __init__.py's own _configure_price_watcher() (issue #256,
native price-triggered solving) -- the optional listener that closes the
timing gap between when a retailer's price sensor state actually updates
and when the periodic 5-minute cron would next pick that update up.

Same testing convention as tests/test_services.py: pytest function-style
tests, asyncio.run() where an async body is genuinely needed, and the
tests/_ha_stubs.py stand-in homeassistant.* modules installed first so
custom_components.nimbus_load imports resolve at all.

Deliberately does NOT touch entities' real state -- the tests here are
scoped exactly to the wiring around async_track_state_change_event: is
it registered when the toggle is on, torn down cleanly when the toggle
flips off, and does the debounce coalesce a burst of state changes into
a single solve. The solve itself is stubbed to an AsyncMock (the same
pattern test_services.py already uses for test_solve_now_calls_async_
run_solve).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()


async def _wait_until(predicate, *, timeout: float = 2.0, step: float = 0.01) -> None:
    """Poll `predicate()` on a real, short interval until it's true or
    `timeout` real seconds have passed.

    nimbus issue #360 (Mark Purcell, codebase review): the two debounce-
    coalescing tests below need a REAL asyncio loop and a REAL
    `hass.loop.call_later()` fire, since that's the actual production
    mechanism being proven (there is no injectable/fake clock to swap in
    for a raw `loop.call_later` call) -- so this isn't a case where a
    real sleep can be eliminated entirely. What CAN be removed is the
    fixed-margin guess (was `await asyncio.sleep(0.15)`, a flat 3x over
    the 0.05s debounce) that assumed 100ms of slack is always enough on
    ANY runner. Polling instead waits exactly as long as the real timer
    actually takes (typically ~1-2 poll steps past the debounce, i.e.
    a few tens of ms) and only fails if the debounced callback genuinely
    never fires within a generous 2s ceiling -- fast on a healthy runner,
    robust on a loaded one, and still a real test of the real timer.
    """
    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            msg = f"condition not met within {timeout}s"
            raise AssertionError(msg)
        await asyncio.sleep(step)
        elapsed += step


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init
from custom_components.nimbus_load import (
    _configure_price_watcher,
    _price_watcher_entities,
    _price_watcher_unload_hooked,
    _price_watcher_unsub,
)
from custom_components.nimbus_load.const import (
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
)


def _fake_entry(entry_id: str, options: dict) -> MagicMock:
    """A fake config entry shaped like every real caller of _configure_
    price_watcher() -- just enough to satisfy .entry_id, .options, and
    .async_on_unload. See tests/test_services.py's own _fake_call/_fake_
    coordinator for the same MagicMock-shaped-object convention.
    """
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = options
    entry.async_on_unload = MagicMock()
    return entry


def _fake_hass() -> MagicMock:
    """A fake hass shaped for _configure_price_watcher(): needs a loop
    (with a real call_later semantics: return a Handle exposing .cancel)
    and async_create_task (called by the debounced _fire_solve, verified
    to be an AsyncMock-compatible stand-in in individual tests).
    """
    hass = MagicMock()
    hass.async_create_task = MagicMock()
    # 2026-08-29 (issue #232 follow-up): _configure_price_watcher() now
    # reads switch.nimbus_solve_on_price_change and
    # number.nimbus_solve_on_price_change_debounce_s FIRST, falling
    # through to entry.options only when the entity hasn't been
    # created yet. Force hass.states.get(...) to return None here so
    # every one of these tests still exercises the entry.options path
    # (the source-of-truth for the fresh-install migration case AND
    # every one of these test scenarios' own toggle-and-debounce
    # inputs).
    hass.states.get = MagicMock(return_value=None)
    return hass


def _reset_module_state() -> None:
    """Every test starts from a clean module-level state, so a stale
    key from a previous test's own entry_id can't fool _configure_
    price_watcher() into thinking a listener is already registered.
    """
    _price_watcher_unsub.clear()
    _price_watcher_entities.clear()
    _price_watcher_unload_hooked.clear()


def test_debounce_coalesces_a_burst_of_state_changes_into_one_solve():
    # test_debounce_coalesces_a_burst_of_state_changes_into_one_solve: This is the real reason the debounce field exists -- Amber's own
    _reset_module_state()

    async def _run() -> None:
        # Use a real asyncio loop for a genuine call_later behaviour,
        # since MagicMock's own call_later returns a MagicMock with no
        # real fire-after-delay semantics.
        loop = asyncio.get_event_loop()
        hass = MagicMock()
        hass.loop = loop
        # Same reason as _fake_hass() above -- force the switch/number
        # state lookups to fall through to entry.options so this test's
        # own CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: 0.05 override is
        # what actually reaches call_later().
        hass.states.get = MagicMock(return_value=None)
        # AsyncMock lets us count "how many solve tasks were kicked off"
        # -- the important assertion here.
        fake_solve = AsyncMock(return_value=True)
        # async_create_task in real HA schedules a coroutine onto the
        # loop; here we just call the coroutine directly so the
        # AsyncMock's own call count increments predictably at fire time.
        hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
        entry = _fake_entry(
            "entry_debounce",
            {
                CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
                CONF_SOLVE_ON_PRICE_CHANGE: True,
                # 50ms -- long enough that three fire-immediately calls
                # from the same coroutine test body all coalesce, short
                # enough for the test itself to finish inside a second.
                CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: 0.05,
            },
        )

        captured: dict[str, object] = {}

        def _capture(_hass, _entities, callback):
            captured["callback"] = callback
            return MagicMock()

        with (
            patch(
                "custom_components.nimbus_load.async_track_state_change_event",
                side_effect=_capture,
            ),
            patch(
                "custom_components.nimbus_load.solver_runtime.async_run_solve",
                fake_solve,
            ),
        ):
            _configure_price_watcher(hass, entry)

            # Fire the captured state-change callback three times back to
            # back, no await in between -- exactly the Amber "correlated
            # burst" pattern.
            cb = captured["callback"]
            fake_event = MagicMock()
            cb(fake_event)
            cb(fake_event)
            cb(fake_event)

            # Wait for the real debounce timer to fire and the coalesced
            # task to actually run -- see _wait_until()'s own docstring
            # for why this polls instead of a fixed sleep.
            await _wait_until(lambda: fake_solve.called)

        fake_solve.assert_called_once_with(hass)

    asyncio.run(_run())

    # test_live_debounce_number_state_overrides_entry_options_when_present
    _reset_module_state()
    hass = _fake_hass()
    switch_state = MagicMock()
    switch_state.state = "on"
    debounce_state = MagicMock()
    debounce_state.state = "12.5"

    def _states_get(entity_id):
        if entity_id == "switch.nimbus_solve_on_price_change":
            return switch_state
        if entity_id == "number.nimbus_solve_on_price_change_debounce_s":
            return debounce_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    entry = _fake_entry(
        "entry_live_debounce",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            # entry.options says 5s, but the live number says 12.5 --
            # live number wins.
            CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: 5.0,
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ):
        _configure_price_watcher(hass, entry)

    hass.loop.call_later.assert_not_called()  # Only fires on state change,
    # not at registration -- what we're asserting is just that the listener
    # got registered and stored, i.e. that a live-number-only test path
    # (no entry.options fallback) still succeeds. call_later would be
    # exercised by the pre-existing coalesce test above.
    assert _price_watcher_unsub.get("entry_live_debounce") is not None

    # test_debounced_solve_records_completion_with_triggering_entity_and_timestamp: Issue #294 (Mark Purcell): the sensor.nimbus_solver_price_response_
    _reset_module_state()

    async def _run() -> None:
        loop = asyncio.get_event_loop()
        hass = MagicMock()
        hass.loop = loop
        hass.states.get = MagicMock(return_value=None)
        hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
        fake_solve = AsyncMock(return_value=True)
        fake_record = MagicMock()
        entry = _fake_entry(
            "entry_latency_record",
            {
                CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
                CONF_SOLVE_ON_PRICE_CHANGE: True,
                CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: 0.05,
            },
        )

        captured: dict[str, object] = {}

        def _capture(_hass, _entities, callback):
            captured["callback"] = callback
            return MagicMock()

        with (
            patch(
                "custom_components.nimbus_load.async_track_state_change_event",
                side_effect=_capture,
            ),
            patch(
                "custom_components.nimbus_load.solver_runtime.async_run_solve",
                fake_solve,
            ),
            patch(
                "custom_components.nimbus_load.solver_runtime.record_solve_completed",
                fake_record,
            ),
        ):
            _configure_price_watcher(hass, entry)

            cb = captured["callback"]
            last_changed = "2026-08-31T07:55:20.370000+00:00"
            fake_event = MagicMock()
            fake_event.data = {
                "entity_id": "sensor.import_a",
                "new_state": SimpleNamespace(last_changed=last_changed),
            }
            cb(fake_event)

            await _wait_until(lambda: fake_solve.called)

        fake_solve.assert_called_once_with(hass)
        fake_record.assert_called_once_with(
            trigger_source="price_change",
            triggering_entity="sensor.import_a",
            price_change_at=last_changed,
            debounce_s=0.05,
        )

    asyncio.run(_run())

    # test_reconfigure_toggles_register_one_unload_hook_per_load: Every dashboard toggle re-runs _configure_price_watcher(). It used
    _reset_module_state()
    hass = _fake_hass()
    entry = _entry_with_watcher("entry_toggle")
    first, second = _OneShotUnsub(), _OneShotUnsub()
    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        side_effect=[first, second],
    ):
        _configure_price_watcher(hass, entry)
        # Simulate the toggle going off, then on again.
        hass.states.get = MagicMock(
            side_effect=lambda eid: (
                MagicMock(state="off") if eid.startswith("switch.") else None
            )
        )
        _configure_price_watcher(hass, entry)
        assert first.calls == 1
        hass.states.get = MagicMock(
            side_effect=lambda eid: (
                MagicMock(state="on") if eid.startswith("switch.") else None
            )
        )
        _configure_price_watcher(hass, entry)
    assert entry.async_on_unload.call_count == 1
    hook = entry.async_on_unload.call_args.args[0]
    nimbus_init._cancel_price_watcher("entry_toggle")
    hook()
    assert second.calls == 1

    # test_combined_unsub_is_idempotent_on_its_own
    _reset_module_state()
    hass = _fake_hass()
    entry = _entry_with_watcher("entry_idem")
    one_shot = _OneShotUnsub()
    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=one_shot,
    ):
        _configure_price_watcher(hass, entry)
    combined = _price_watcher_unsub["entry_idem"]
    combined()
    combined()
    assert one_shot.calls == 1


# 2026-08-29 (issue #232 follow-up): the toggle and debounce moved out
# to a live switch.nimbus_solve_on_price_change and
# number.nimbus_solve_on_price_change_debounce_s -- the tests below
# assert that _configure_price_watcher() reads those live entities
# FIRST, so a dashboard toggle takes effect with no hub reload, while
# still falling through to entry.options on installs whose switch/
# number entities haven't been created yet (the fresh-install migration
# path exercised by every test above).


# -- nimbus issue #633 (Mark Purcell): attribute-only refreshes must not
# be measured as if they were a real price change --


# -- nimbus issue #337: the state-change unsub is NOT safe to call twice --


class _OneShotUnsub:
    """Mimics HA core's async_track_state_change_event unsub: the second
    call does `callbacks[key].remove(job)` on an already-emptied
    defaultdict list and raises ValueError."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls > 1:
            raise ValueError("list.remove(x): x not in list")


def _entry_with_watcher(entry_id: str):
    return _fake_entry(
        entry_id,
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE: True,
        },
    )
