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
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

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


def test_one_coordinator_failing_does_not_abort_the_others():
    """nimbus issue #365 (Mark Purcell): asyncio.gather() without
    return_exceptions=True aborts EVERY other in-flight coordinate the
    moment one raises, so a service call retraining 18 loads could fail
    with one opaque error while some loads never even started. coord_b
    and coord_c must both still be awaited even though coord_a's own
    _async_retrain() raises."""
    coord_a = _fake_coordinator()
    coord_a._async_retrain = AsyncMock(side_effect=RuntimeError("boom"))
    coord_b = _fake_coordinator()
    coord_c = _fake_coordinator()
    hass = _fake_hass_with_coordinators({"a": coord_a, "b": coord_b, "c": coord_c})
    call = _fake_call({})

    # Must not raise -- return_exceptions=True means gather() itself
    # never propagates any one coordinator's own failure.
    asyncio.run(services._async_handle_retrain(hass, call))

    coord_a._async_retrain.assert_called_once()
    coord_b._async_retrain.assert_called_once()
    coord_c._async_retrain.assert_called_once()


def test_handle_retrain_with_no_entity_id_and_no_coordinators_is_a_noop(caplog):
    # nimbus issue #360 (Mark Purcell, codebase review): "must not raise"
    # alone is a blind assertion -- it passes just as well if this whole
    # branch (the `if not targets:` warning) silently stopped being
    # reached at all as it does for the real, intended "genuinely a
    # no-op" behaviour. Asserting the real WARNING it's supposed to log
    # actually fired closes that gap.
    hass = _fake_hass_with_coordinators({})
    call = _fake_call({})

    # Must not raise -- "nothing configured yet" is a real, valid state
    # for a fresh install, not an error.
    with caplog.at_level("WARNING"):
        asyncio.run(services._async_handle_retrain(hass, call))
    assert "no entity_id and no" in caplog.text


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


def test_async_unregister_services_removes_all_three():
    """nimbus issue #365 (Mark Purcell, codebase review), item 1: removing
    the (only, single_config_entry) hub used to leave all three services
    registered and callable forever."""
    hass = MagicMock()
    hass.services.has_service.return_value = True

    services.async_unregister_services(hass)

    removed = {call.args[1] for call in hass.services.async_remove.call_args_list}
    assert removed == {
        services.SERVICE_RETRAIN,
        services.SERVICE_SOLVE_NOW,
        services.SERVICE_COMPUTE_QUALITY_REPORT,
    }
    for call in hass.services.async_remove.call_args_list:
        assert call.args[0] == services.DOMAIN


def test_async_unregister_services_is_a_safe_no_op_when_nothing_registered():
    hass = MagicMock()
    hass.services.has_service.return_value = False

    services.async_unregister_services(hass)

    hass.services.async_remove.assert_not_called()


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


def test_solve_now_logs_a_warning_on_a_failed_solve_but_does_not_raise(caplog):
    """async_run_solve()'s own contract is "never raises, returns False
    on any handled failure" -- the service handler must respect that
    same contract, not treat a False return as something to propagate
    as an exception (which would surface as a confusing generic HA
    service-call error instead of the real, already-descriptive status
    already sitting on sensor.nimbus_solver_battery_forecast).

    nimbus issue #360 (Mark Purcell, codebase review): the function's
    own name promises a warning gets logged, but the original test never
    checked for one -- "must not raise" alone passes just as well if the
    `if not ok:` branch silently stopped logging (or stopped being
    reached) as it does for the real, intended behaviour.
    """
    hass = MagicMock()
    call = _fake_call({})
    services.solver_runtime.async_run_solve = AsyncMock(return_value=False)

    with caplog.at_level("WARNING"):
        asyncio.run(services._async_handle_solve_now(hass, call))  # must not raise
    assert "did not produce a successful solve" in caplog.text


# --- _coerce_datetime (nimbus issue #345) -----------------------------------
#
# tests/_ha_stubs.py's own homeassistant.util.dt is a bare MagicMock (no real
# datetime math) -- these tests patch services.dt_util's three functions with
# real, self-contained implementations (real datetime/zoneinfo arithmetic,
# not a reimplementation of _coerce_datetime's own logic) so the actual
# normalisation behaviour is genuinely verified, not just "some MagicMock
# was called".


def _patch_real_dt_util(tz):
    from datetime import UTC
    from unittest.mock import patch

    return (
        patch.object(services.dt_util, "DEFAULT_TIME_ZONE", tz),
        patch.object(
            services.dt_util, "parse_datetime", side_effect=datetime.fromisoformat
        ),
        patch.object(
            services.dt_util, "as_utc", side_effect=lambda d: d.astimezone(UTC)
        ),
    )


def test_coerce_datetime_anchors_a_naive_string_to_ha_local_time_then_converts_to_utc():
    from datetime import UTC
    from zoneinfo import ZoneInfo

    brisbane = ZoneInfo("Australia/Brisbane")  # UTC+10, no DST
    patches = _patch_real_dt_util(brisbane)
    with patches[0], patches[1], patches[2]:
        result = services._coerce_datetime("2026-09-01T00:00:00")

    assert result.tzinfo is not None
    assert result == datetime(2026, 8, 31, 14, 0, 0, tzinfo=UTC)


def test_coerce_datetime_anchors_a_naive_datetime_object_to_ha_local_time():
    """Real bug: HA's own `datetime:` selector in services.yaml hands the
    handler an ALREADY-PARSED datetime object, not a string -- confirmed
    this used to return it completely unmodified, naive tzinfo included."""
    from datetime import UTC
    from zoneinfo import ZoneInfo

    brisbane = ZoneInfo("Australia/Brisbane")
    patches = _patch_real_dt_util(brisbane)
    with patches[0], patches[1], patches[2]:
        # Deliberately naive -- this is exactly the input shape under test.
        result = services._coerce_datetime(datetime(2026, 9, 1, 0, 0, 0))  # noqa: DTZ001

    assert result.tzinfo is not None
    assert result == datetime(2026, 8, 31, 14, 0, 0, tzinfo=UTC)


def test_coerce_datetime_leaves_an_already_aware_string_alone_besides_utc_conversion():
    from datetime import UTC
    from zoneinfo import ZoneInfo

    brisbane = ZoneInfo("Australia/Brisbane")
    patches = _patch_real_dt_util(brisbane)
    with patches[0], patches[1], patches[2]:
        result = services._coerce_datetime("2026-09-01T00:00:00+10:00")

    # Already carried its own explicit offset -- must NOT be re-anchored to
    # the (irrelevant, since it was already aware) DEFAULT_TIME_ZONE.
    assert result == datetime(2026, 8, 31, 14, 0, 0, tzinfo=UTC)


def test_coerce_datetime_rejects_an_unparseable_string():
    from unittest.mock import patch

    with (
        patch.object(services.dt_util, "parse_datetime", return_value=None),
        pytest.raises(vol.Invalid),
    ):
        services._coerce_datetime("not a real datetime")


def test_coerce_datetime_rejects_a_non_datetime_non_string_value():
    with pytest.raises(vol.Invalid):
        services._coerce_datetime(12345)
