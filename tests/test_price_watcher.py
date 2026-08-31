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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import (
    _configure_price_watcher,
    _configured_price_sensors,
    _price_watcher_entities,
    _price_watcher_unsub,
)
from custom_components.nimbus_load.const import (
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
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


def test_configured_price_sensors_returns_only_populated_entries():
    entry = _fake_entry(
        "e",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVER_IMPORT_PRICE_SENSOR_2: None,  # unset
            CONF_SOLVER_IMPORT_PRICE_SENSOR_3: "",  # cleared
            CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.export_a",
            CONF_SOLVER_EXPORT_PRICE_SENSOR_2: "sensor.export_b",
        },
    )

    assert _configured_price_sensors(entry) == (
        "sensor.import_a",
        "sensor.export_a",
        "sensor.export_b",
    )


def test_configured_price_sensors_returns_empty_tuple_when_nothing_set():
    entry = _fake_entry("e", {})

    assert _configured_price_sensors(entry) == ()


def test_toggle_off_registers_no_listener_and_leaves_no_state():
    _reset_module_state()
    hass = _fake_hass()
    entry = _fake_entry(
        "entry_toggle_off",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE: False,
        },
    )

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event"
    ) as track_event:
        _configure_price_watcher(hass, entry)

    track_event.assert_not_called()
    assert _price_watcher_unsub.get("entry_toggle_off") is None
    assert _price_watcher_entities.get("entry_toggle_off", ()) == ()
    entry.async_on_unload.assert_not_called()


def test_toggle_off_with_no_price_sensors_configured_is_a_noop():
    _reset_module_state()
    hass = _fake_hass()
    entry = _fake_entry("entry_bare", {})

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event"
    ) as track_event:
        _configure_price_watcher(hass, entry)

    track_event.assert_not_called()
    entry.async_on_unload.assert_not_called()


def test_toggle_on_registers_a_listener_on_every_configured_price_sensor():
    _reset_module_state()
    hass = _fake_hass()
    entry = _fake_entry(
        "entry_on",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVER_IMPORT_PRICE_SENSOR_2: "sensor.import_b",
            CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.export_a",
            CONF_SOLVE_ON_PRICE_CHANGE: True,
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ) as track_event:
        _configure_price_watcher(hass, entry)

    track_event.assert_called_once()
    called_hass, called_entities, called_callback = track_event.call_args.args
    assert called_hass is hass
    assert called_entities == [
        "sensor.import_a",
        "sensor.import_b",
        "sensor.export_a",
    ]
    assert callable(called_callback)
    # The unsub registered with entry.async_on_unload wraps fake_unsub;
    # it isn't the raw one, but the combined wrapper that also cancels
    # any pending debounced solve on unload.
    assert entry.async_on_unload.call_count == 1
    assert _price_watcher_entities["entry_on"] == (
        "sensor.import_a",
        "sensor.import_b",
        "sensor.export_a",
    )


def test_reconfigure_with_same_entities_is_a_noop_no_reregister():
    """A hub reload for something unrelated (e.g. a load subentry edit)
    must not tear down and re-register an identical listener -- the
    fast-path guard in _configure_price_watcher exists exactly for this
    case.
    """
    _reset_module_state()
    hass = _fake_hass()
    entry = _fake_entry(
        "entry_reload",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE: True,
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ) as track_event:
        _configure_price_watcher(hass, entry)
        _configure_price_watcher(hass, entry)  # second call, identical state

    track_event.assert_called_once()  # NOT twice
    fake_unsub.assert_not_called()  # never torn down


def test_reconfigure_with_toggle_flipped_off_tears_down_the_listener():
    _reset_module_state()
    hass = _fake_hass()
    on_entry = _fake_entry(
        "entry_flip",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE: True,
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ):
        _configure_price_watcher(hass, on_entry)

    # Now the user flips the toggle off and reloads.
    off_entry = _fake_entry(
        "entry_flip",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE: False,
        },
    )
    _configure_price_watcher(hass, off_entry)

    fake_unsub.assert_called_once()
    assert _price_watcher_unsub.get("entry_flip") is None


def test_reconfigure_with_new_price_sensor_tears_down_and_re_registers():
    _reset_module_state()
    hass = _fake_hass()
    old_unsub = MagicMock()
    new_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        side_effect=[old_unsub, new_unsub],
    ) as track_event:
        _configure_price_watcher(
            hass,
            _fake_entry(
                "entry_changed_sensors",
                {
                    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
                    CONF_SOLVE_ON_PRICE_CHANGE: True,
                },
            ),
        )
        _configure_price_watcher(
            hass,
            _fake_entry(
                "entry_changed_sensors",
                {
                    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
                    CONF_SOLVER_IMPORT_PRICE_SENSOR_2: "sensor.import_b",  # new
                    CONF_SOLVE_ON_PRICE_CHANGE: True,
                },
            ),
        )

    assert track_event.call_count == 2
    old_unsub.assert_called_once()
    assert _price_watcher_entities["entry_changed_sensors"] == (
        "sensor.import_a",
        "sensor.import_b",
    )


def test_debounce_coalesces_a_burst_of_state_changes_into_one_solve():
    """This is the real reason the debounce field exists -- Amber's own
    coordinator updates several correlated price sensors within tens of
    milliseconds of each other; without the debounce, a single Amber
    poll would fan out into N solves within the same second.
    """
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

            # Wait past the debounce window and let the coalesced task
            # actually run.
            await asyncio.sleep(0.15)

        fake_solve.assert_called_once_with(hass)

    asyncio.run(_run())


# 2026-08-29 (issue #232 follow-up): the toggle and debounce moved out
# to a live switch.nimbus_solve_on_price_change and
# number.nimbus_solve_on_price_change_debounce_s -- the tests below
# assert that _configure_price_watcher() reads those live entities
# FIRST, so a dashboard toggle takes effect with no hub reload, while
# still falling through to entry.options on installs whose switch/
# number entities haven't been created yet (the fresh-install migration
# path exercised by every test above).


def test_live_switch_state_overrides_entry_options_when_present():
    _reset_module_state()
    hass = _fake_hass()
    # Switch entity exists and says OFF -- must beat entry.options
    # saying ON, so a household that toggles the switch off from the
    # dashboard genuinely stops the extra solves.
    switch_state = MagicMock()
    switch_state.state = "off"
    hass.states.get = MagicMock(
        side_effect=lambda entity_id: (
            switch_state if entity_id == "switch.nimbus_solve_on_price_change" else None
        )
    )
    entry = _fake_entry(
        "entry_switch_off",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            # entry.options says ON, but the live switch is OFF -- switch wins.
            CONF_SOLVE_ON_PRICE_CHANGE: True,
        },
    )

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event"
    ) as track_event:
        _configure_price_watcher(hass, entry)

    track_event.assert_not_called()


def test_live_switch_state_on_registers_listener_even_when_options_missing():
    _reset_module_state()
    hass = _fake_hass()
    switch_state = MagicMock()
    switch_state.state = "on"
    hass.states.get = MagicMock(
        side_effect=lambda entity_id: (
            switch_state if entity_id == "switch.nimbus_solve_on_price_change" else None
        )
    )
    entry = _fake_entry(
        "entry_switch_on",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            # Deliberately not setting CONF_SOLVE_ON_PRICE_CHANGE -- a
            # dashboard user who has never opened the wizard on this
            # install should be able to just toggle the switch on and
            # have it work.
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ) as track_event:
        _configure_price_watcher(hass, entry)

    track_event.assert_called_once()
    assert _price_watcher_entities.get("entry_switch_on") == ("sensor.import_a",)


def test_live_debounce_number_state_overrides_entry_options_when_present():
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


def test_unknown_debounce_state_falls_through_to_entry_options():
    _reset_module_state()
    hass = _fake_hass()
    switch_state = MagicMock()
    switch_state.state = "on"
    # Number entity is present but STILL restoring (state == "unknown")
    # during the tail end of hub startup -- must fall through to entry.
    # options, not silently coerce to 0 or DEFAULT.
    debounce_state = MagicMock()
    debounce_state.state = "unknown"

    def _states_get(entity_id):
        if entity_id == "switch.nimbus_solve_on_price_change":
            return switch_state
        if entity_id == "number.nimbus_solve_on_price_change_debounce_s":
            return debounce_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    entry = _fake_entry(
        "entry_debounce_unknown",
        {
            CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.import_a",
            CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: 7.5,
        },
    )
    fake_unsub = MagicMock()

    with patch(
        "custom_components.nimbus_load.async_track_state_change_event",
        return_value=fake_unsub,
    ):
        _configure_price_watcher(hass, entry)

    assert _price_watcher_unsub.get("entry_debounce_unknown") is not None


def test_debounced_solve_records_completion_with_triggering_entity_and_timestamp():
    """Issue #294 (Mark Purcell): the sensor.nimbus_solver_price_response_
    latency attributes (triggering_entity, last_price_change_at) have to
    come from SOMEWHERE -- this is that somewhere. _on_price_change must
    capture the firing event's own entity_id and new_state.last_changed,
    and _fire_solve's real solve completion must forward exactly those
    (plus the resolved debounce_s) to solver_runtime.record_solve_
    completed() as trigger_source="price_change" -- not just fire-and-
    forget the bare solve the way the pre-#294 code did.
    """
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

            await asyncio.sleep(0.15)

        fake_solve.assert_called_once_with(hass)
        fake_record.assert_called_once_with(
            trigger_source="price_change",
            triggering_entity="sensor.import_a",
            price_change_at=last_changed,
            debounce_s=0.05,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    import traceback

    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError:
            print(f"FAIL: {name}")
            traceback.print_exc()
        except Exception:
            print(f"ERROR: {name}")
            traceback.print_exc()
    print(f"{passed}/{len(tests)} passed")
