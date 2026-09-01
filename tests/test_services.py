"""Real test of services.py (nimbus_load.retrain, issue #195) -- the service
Mark Purcell asked for so an integration update that changes the training
logic can actually be verified without waiting up to 24h for the next
scheduled retrain, or deleting the persisted .pkl and restarting HA.

Imports and exercises the REAL module (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules -- same pattern
test_diagnostics.py already established for this file's own HA-importing
modules.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.exceptions import ServiceValidationError

from custom_components.nimbus_load import services


def _fake_call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _fake_coordinator() -> MagicMock:
    c = MagicMock()
    # AsyncMock, not a pre-built Future -- a Future is bound to whatever
    # loop existed at construction time, which is not necessarily the
    # loop asyncio.run() creates for the actual test body, and raises
    # "attached to a different loop" the moment gather() awaits it.
    # AsyncMock's own coroutine is created fresh on each call, correctly
    # picking up whatever loop is actually running at await time.
    c._async_retrain = AsyncMock()
    return c


def _fake_hass_with_coordinators(coordinators: dict) -> MagicMock:
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = coordinators
    hass.config_entries.async_entries.return_value = [entry]
    return hass


def test_all_coordinators_merges_across_every_hub_entry():
    coord_a = _fake_coordinator()
    coord_b = _fake_coordinator()
    hass = MagicMock()
    entry1 = MagicMock()
    entry1.runtime_data = {"sub1": coord_a}
    entry2 = MagicMock()
    entry2.runtime_data = {"sub2": coord_b}
    hass.config_entries.async_entries.return_value = [entry1, entry2]

    result = services._all_coordinators(hass)

    assert result == {"sub1": coord_a, "sub2": coord_b}


def test_all_coordinators_tolerates_an_entry_with_no_runtime_data_yet():
    hass = MagicMock()
    entry = MagicMock()
    entry.runtime_data = None
    hass.config_entries.async_entries.return_value = [entry]

    assert services._all_coordinators(hass) == {}


def test_coordinator_for_entity_id_resolves_a_load_forecast_entity():
    coord = _fake_coordinator()
    hass = MagicMock()
    registry_entry = MagicMock()
    registry_entry.unique_id = "sub123_load_forecast"
    registry = MagicMock()
    registry.async_get.return_value = registry_entry
    services.er.async_get = MagicMock(return_value=registry)

    resolved = services._coordinator_for_entity_id(
        hass, "sensor.pool_forecast", {"sub123": coord}
    )

    assert resolved is coord


def test_coordinator_for_entity_id_resolves_a_signal_forecast_entity():
    coord = _fake_coordinator()
    hass = MagicMock()
    registry_entry = MagicMock()
    registry_entry.unique_id = "subABC_signal_forecast"
    registry = MagicMock()
    registry.async_get.return_value = registry_entry
    services.er.async_get = MagicMock(return_value=registry)

    resolved = services._coordinator_for_entity_id(
        hass, "sensor.battery_forecast", {"subABC": coord}
    )

    assert resolved is coord


def test_coordinator_for_entity_id_returns_none_for_unregistered_entity():
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = None
    services.er.async_get = MagicMock(return_value=registry)

    assert services._coordinator_for_entity_id(hass, "sensor.nope", {}) is None


def test_coordinator_for_entity_id_returns_none_for_a_non_nimbus_entity():
    # Registered, but its unique_id doesn't end in a Nimbus forecast suffix
    # (e.g. a completely unrelated entity someone passed by mistake).
    hass = MagicMock()
    registry_entry = MagicMock()
    registry_entry.unique_id = "some_other_integrations_unique_id"
    registry = MagicMock()
    registry.async_get.return_value = registry_entry
    services.er.async_get = MagicMock(return_value=registry)

    assert services._coordinator_for_entity_id(hass, "sensor.unrelated", {}) is None


def test_handle_retrain_with_no_entity_id_retrains_every_coordinator():
    coord_a = _fake_coordinator()
    coord_b = _fake_coordinator()
    hass = _fake_hass_with_coordinators({"a": coord_a, "b": coord_b})
    call = _fake_call({})

    asyncio.run(services._async_handle_retrain(hass, call))

    coord_a._async_retrain.assert_called_once()
    coord_b._async_retrain.assert_called_once()


def test_handle_retrain_with_no_entity_id_and_no_coordinators_is_a_noop():
    hass = _fake_hass_with_coordinators({})
    call = _fake_call({})

    # Must not raise -- "nothing configured yet" is a real, valid state
    # for a fresh install, not an error.
    asyncio.run(services._async_handle_retrain(hass, call))


def test_handle_retrain_with_specific_entity_id_retrains_only_that_one():
    target = _fake_coordinator()
    other = _fake_coordinator()
    hass = _fake_hass_with_coordinators({"target_sub": target, "other_sub": other})
    registry_entry = MagicMock()
    registry_entry.unique_id = "target_sub_load_forecast"
    registry = MagicMock()
    registry.async_get.return_value = registry_entry
    services.er.async_get = MagicMock(return_value=registry)

    call = _fake_call({"entity_id": ["sensor.target_forecast"]})
    asyncio.run(services._async_handle_retrain(hass, call))

    target._async_retrain.assert_called_once()
    other._async_retrain.assert_not_called()


def test_handle_retrain_raises_service_validation_error_for_unresolved_entity():
    hass = _fake_hass_with_coordinators({})
    registry = MagicMock()
    registry.async_get.return_value = None
    services.er.async_get = MagicMock(return_value=registry)

    call = _fake_call({"entity_id": ["sensor.not_a_nimbus_entity"]})

    with pytest.raises(ServiceValidationError):
        asyncio.run(services._async_handle_retrain(hass, call))


def test_handle_retrain_raises_for_partial_match_and_retrains_nothing():
    # A real, deliberate design choice worth locking in with a test: if
    # ONE of several requested entity_ids can't be resolved, the whole
    # call fails loudly rather than silently retraining only the ones
    # that did resolve -- a user asking to retrain 3 loads and getting 2
    # with no error would be a much worse debugging experience than a
    # clear ServiceValidationError naming exactly which one didn't match.
    good = _fake_coordinator()
    hass = _fake_hass_with_coordinators({"good_sub": good})
    good_entry = MagicMock()
    good_entry.unique_id = "good_sub_load_forecast"

    def fake_async_get(entity_id):
        return good_entry if entity_id == "sensor.good_forecast" else None

    registry = MagicMock()
    registry.async_get.side_effect = fake_async_get
    services.er.async_get = MagicMock(return_value=registry)

    call = _fake_call({"entity_id": ["sensor.good_forecast", "sensor.bad_forecast"]})

    with pytest.raises(ServiceValidationError, match="sensor.bad_forecast"):
        asyncio.run(services._async_handle_retrain(hass, call))

    good._async_retrain.assert_not_called()


def test_async_register_services_registers_both_load_and_signal():
    """Every service gets registered on a fresh setup: retrain (issue
    #195), solve_now (issue #232), and compute_quality_report (issue
    #316). The idempotency guard is exercised by the sibling
    _is_idempotent_on_reload test.
    """
    hass = MagicMock()
    hass.services.has_service.return_value = False

    services.async_register_services(hass)

    assert hass.services.async_register.call_count == 3
    registered_names = {
        call.args[1] for call in hass.services.async_register.call_args_list
    }
    assert registered_names == {
        services.SERVICE_RETRAIN,
        services.SERVICE_SOLVE_NOW,
        services.SERVICE_COMPUTE_QUALITY_REPORT,
    }
    for call in hass.services.async_register.call_args_list:
        assert call.args[0] == services.DOMAIN


def test_async_register_services_is_idempotent_on_reload():
    hass = MagicMock()
    hass.services.has_service.return_value = True

    services.async_register_services(hass)

    hass.services.async_register.assert_not_called()


def test_solve_now_calls_async_run_solve():
    """The whole point of #232's own suggestion -- reuses the exact same
    solve path the periodic timer calls, not a separate implementation.
    """
    hass = MagicMock()
    call = _fake_call({})
    fake_run_solve = AsyncMock(return_value=True)
    services.solver_runtime.async_run_solve = fake_run_solve

    asyncio.run(services._async_handle_solve_now(hass, call))

    fake_run_solve.assert_called_once_with(hass)


def test_solve_now_logs_a_warning_on_a_failed_solve_but_does_not_raise():
    """async_run_solve()'s own contract is "never raises, returns False
    on any handled failure" -- the service handler must respect that
    same contract, not treat a False return as something to propagate
    as an exception (which would surface as a confusing generic HA
    service-call error instead of the real, already-descriptive status
    already sitting on sensor.nimbus_solver_battery_forecast).
    """
    hass = MagicMock()
    call = _fake_call({})
    services.solver_runtime.async_run_solve = AsyncMock(return_value=False)

    asyncio.run(services._async_handle_solve_now(hass, call))  # must not raise


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
