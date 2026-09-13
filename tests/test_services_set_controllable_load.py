"""Real test of nimbus_load.set_controllable_load (nimbus issue #809) --
a single-call, atomic alternative to the config_subentries wizard for a
Controllable Load, built after this session found the wizard itself
fragile in two separate, real, live ways migrating Mark Purcell's own
"Hot Water Heat Pump" load to kind=thermal: an "Add" tap that looks
identical to "Reconfigure" (would have created a silent duplicate), and
optional fields that read as filled in the form but did not actually
persist, confirmed only by re-solving and reverse-engineering the LP's
own fallback behaviour.

Same MagicMock-hass, call-the-handler-directly pattern already
established in test_services.py for retrain/solve_now/
compute_quality_report -- these tests exercise the handler's own
create-vs-update/ambiguity/error-shape logic, not voluptuous's schema
validation (which HA's own service dispatch applies before the handler
ever runs, same as every other service in this file).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.nimbus_load import services
from custom_components.nimbus_load.const import SUBENTRY_TYPE_CONTROLLABLE_LOAD


def _fake_call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _fake_subentry(
    subentry_id: str, title: str, subentry_type: str = SUBENTRY_TYPE_CONTROLLABLE_LOAD
):
    subentry = MagicMock()
    subentry.subentry_id = subentry_id
    subentry.title = title
    subentry.subentry_type = subentry_type
    return subentry


def _fake_hass_with_entry(subentries: dict, entry_id: str = "entry1") -> tuple:
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.subentries = subentries
    hass.config_entries.async_entries.return_value = [entry]
    hass.config_entries.async_reload = AsyncMock()
    return hass, entry


def test_set_controllable_load_creates_a_new_subentry_when_no_name_matches():
    hass, entry = _fake_hass_with_entry({})
    created_subentry = MagicMock()
    created_subentry.subentry_id = "new_id_123"
    fake_config_subentry_cls = MagicMock(return_value=created_subentry)
    call = _fake_call(
        {"controllable_load_name": "Pool Pump", "controllable_load_kind": "sheddable"}
    )

    with patch.object(services, "ConfigSubentry", fake_config_subentry_cls):
        result = asyncio.run(services._async_handle_set_controllable_load(hass, call))

    fake_config_subentry_cls.assert_called_once()
    _, kwargs = fake_config_subentry_cls.call_args
    assert kwargs["title"] == "Pool Pump"
    assert kwargs["subentry_type"] == SUBENTRY_TYPE_CONTROLLABLE_LOAD
    hass.config_entries.async_add_subentry.assert_called_once_with(
        entry, created_subentry
    )
    hass.config_entries.async_update_subentry.assert_not_called()
    hass.config_entries.async_reload.assert_awaited_once_with("entry1")
    assert result["created"] is True
    assert result["subentry_id"] == "new_id_123"
    assert result["name"] == "Pool Pump"
    assert result["kind"] == "sheddable"


def test_set_controllable_load_updates_the_existing_subentry_matched_by_title():
    existing = _fake_subentry("existing_id", "Hot Water Heat Pump")
    hass, entry = _fake_hass_with_entry({"existing_id": existing})
    call = _fake_call(
        {
            "controllable_load_name": "Hot Water Heat Pump",
            "controllable_load_kind": "thermal",
        }
    )

    result = asyncio.run(services._async_handle_set_controllable_load(hass, call))

    hass.config_entries.async_update_subentry.assert_called_once_with(
        entry,
        existing,
        title="Hot Water Heat Pump",
        data={
            "controllable_load_name": "Hot Water Heat Pump",
            "controllable_load_kind": "thermal",
        },
    )
    hass.config_entries.async_add_subentry.assert_not_called()
    assert result["created"] is False
    assert result["subentry_id"] == "existing_id"


def test_set_controllable_load_updates_by_explicit_subentry_id_even_if_title_differs():
    """nimbus issue #809's own root cause: a title match alone is exactly
    what a retyped/rewhitespaced name ("Hot Water Heat Pump" vs "Hot
    water Heat Pump ") silently defeats. subentry_id is the unambiguous
    escape hatch -- and updating it also renames the subentry to the new
    title, matching the wizard's own reconfigure-with-a-new-name
    behaviour."""
    existing = _fake_subentry("hws_id", "Hot water Heat Pump ")
    hass, entry = _fake_hass_with_entry({"hws_id": existing})
    call = _fake_call(
        {
            "controllable_load_name": "Hot Water Heat Pump",
            "controllable_load_kind": "thermal",
            "subentry_id": "hws_id",
        }
    )

    result = asyncio.run(services._async_handle_set_controllable_load(hass, call))

    hass.config_entries.async_update_subentry.assert_called_once_with(
        entry,
        existing,
        title="Hot Water Heat Pump",
        data={
            "controllable_load_name": "Hot Water Heat Pump",
            "controllable_load_kind": "thermal",
        },
    )
    assert result["subentry_id"] == "hws_id"


def test_set_controllable_load_raises_for_an_unknown_subentry_id():
    hass, _entry = _fake_hass_with_entry({})
    call = _fake_call(
        {
            "controllable_load_name": "Pool Pump",
            "subentry_id": "does_not_exist",
        }
    )

    with pytest.raises(ServiceValidationError, match="does_not_exist"):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))

    hass.config_entries.async_reload.assert_not_awaited()


def test_set_controllable_load_raises_for_a_subentry_id_of_the_wrong_type():
    wrong_type = _fake_subentry(
        "battery1", "ev_m3p", subentry_type="battery_participant"
    )
    hass, _entry = _fake_hass_with_entry({"battery1": wrong_type})
    call = _fake_call(
        {"controllable_load_name": "Pool Pump", "subentry_id": "battery1"}
    )

    with pytest.raises(ServiceValidationError, match="battery1"):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))


def test_set_controllable_load_raises_on_ambiguous_title_match_without_subentry_id():
    dup_a = _fake_subentry("id_a", "Hot Water Heat Pump")
    dup_b = _fake_subentry("id_b", "Hot Water Heat Pump")
    hass, _entry = _fake_hass_with_entry({"id_a": dup_a, "id_b": dup_b})
    call = _fake_call({"controllable_load_name": "Hot Water Heat Pump"})

    with pytest.raises(ServiceValidationError, match="2 existing"):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))

    hass.config_entries.async_update_subentry.assert_not_called()
    hass.config_entries.async_reload.assert_not_awaited()


def test_set_controllable_load_raises_for_a_missing_name():
    hass, _entry = _fake_hass_with_entry({})
    call = _fake_call({"controllable_load_kind": "sheddable"})

    with pytest.raises(ServiceValidationError, match="controllable_load_name"):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))


def test_set_controllable_load_raises_for_an_empty_name():
    hass, _entry = _fake_hass_with_entry({})
    call = _fake_call({"controllable_load_name": ""})

    with pytest.raises(ServiceValidationError):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))


def test_set_controllable_load_raises_when_no_hub_is_configured():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    call = _fake_call({"controllable_load_name": "Pool Pump"})

    with pytest.raises(HomeAssistantError, match="no Nimbus hub"):
        asyncio.run(services._async_handle_set_controllable_load(hass, call))


def test_set_controllable_load_pops_subentry_id_out_of_the_persisted_data():
    """subentry_id is this service's own routing field, not a real
    controllable_load config key -- it must never leak into what actually
    gets persisted onto the subentry (solver_writer.py has no idea what
    to do with it, and the wizard's own schema doesn't carry it either)."""
    existing = _fake_subentry("hws_id", "Hot Water Heat Pump")
    hass, _entry = _fake_hass_with_entry({"hws_id": existing})
    call = _fake_call(
        {
            "controllable_load_name": "Hot Water Heat Pump",
            "subentry_id": "hws_id",
        }
    )

    asyncio.run(services._async_handle_set_controllable_load(hass, call))

    _, kwargs = hass.config_entries.async_update_subentry.call_args
    assert "subentry_id" not in kwargs["data"]


# Registration/unregistration coverage (all four services, including this
# one) lives in test_services.py's own
# test_async_register_services_registers_both_load_and_signal /
# test_async_unregister_services_removes_all_three -- not duplicated here.
