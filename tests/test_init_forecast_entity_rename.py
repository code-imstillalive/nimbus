"""Real test of _async_rename_stale_forecast_entities (custom_components/
nimbus_load/__init__.py) -- task #99, the "reconfiguring a load/signal's
source sensor leaves its forecast entity_id stuck at the old name" fix.

Imports and exercises the REAL function (not a reimplementation) against
mock hass/registry/subentry objects shaped like real Home Assistant
objects, via tests/_ha_stubs.py's stand-in homeassistant.* modules --
the real `homeassistant` package isn't installed in this project's local
dev environment.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import (
    _async_rename_stale_forecast_entities,
)
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)


def _make_subentry(subentry_id: str, subentry_type: str, source_sensor: str):
    sub = MagicMock()
    sub.subentry_id = subentry_id
    sub.subentry_type = subentry_type
    sub.data = {CONF_LOAD_SENSOR: source_sensor}
    return sub


def _make_registry(entity_id_lookup: dict, existing_entities: set) -> MagicMock:
    """entity_id_lookup: {(domain, platform, unique_id): entity_id or None}
    existing_entities: entity_ids that already exist (async_get(x) is truthy)
    """
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, uid: (
        entity_id_lookup.get((domain, platform, uid))
    )
    registry.async_get.side_effect = lambda entity_id: (
        object() if entity_id in existing_entities else None
    )
    return registry


def _run(hass, entry, registry) -> MagicMock:
    import custom_components.nimbus_load as nimbus_init

    nimbus_init.er.async_get = MagicMock(return_value=registry)
    asyncio.run(_async_rename_stale_forecast_entities(hass, entry))
    return registry


def test_stale_entity_id_gets_renamed_when_target_is_free():
    sub = _make_subentry(
        "abc123", SUBENTRY_TYPE_SIGNAL, "sensor.cb_total_combined_power_adjusted_kw"
    )
    entry = MagicMock()
    entry.subentries = {"abc123": sub}
    old_id = "sensor.nimbus_logger_load_power_forecast"
    registry = _make_registry(
        entity_id_lookup={("sensor", DOMAIN, "abc123_signal_forecast"): old_id},
        existing_entities=set(),
    )
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    registry.async_update_entity.assert_called_once_with(
        old_id,
        new_entity_id="sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast",
    )


def test_already_correct_entity_id_is_a_no_op():
    sub = _make_subentry("abc123", SUBENTRY_TYPE_SIGNAL, "sensor.logger_load_power")
    entry = MagicMock()
    entry.subentries = {"abc123": sub}
    correct_id = "sensor.nimbus_logger_load_power_forecast"
    registry = _make_registry(
        entity_id_lookup={("sensor", DOMAIN, "abc123_signal_forecast"): correct_id},
        existing_entities=set(),
    )
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    registry.async_update_entity.assert_not_called()


def test_collision_with_existing_entity_blocks_the_rename():
    sub = _make_subentry(
        "abc123", SUBENTRY_TYPE_SIGNAL, "sensor.cb_total_combined_power_adjusted_kw"
    )
    entry = MagicMock()
    entry.subentries = {"abc123": sub}
    old_id = "sensor.nimbus_logger_load_power_forecast"
    target_id = "sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast"
    registry = _make_registry(
        entity_id_lookup={("sensor", DOMAIN, "abc123_signal_forecast"): old_id},
        existing_entities={
            target_id
        },  # something else already lives at the target name
    )
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    registry.async_update_entity.assert_not_called()


def test_not_yet_registered_is_skipped_not_an_error():
    sub = _make_subentry("abc123", SUBENTRY_TYPE_LOAD, "sensor.sw25_hws_l1_temperature")
    entry = MagicMock()
    entry.subentries = {"abc123": sub}
    registry = _make_registry(
        entity_id_lookup={},  # nothing registered yet -- first-ever setup
        existing_entities=set(),
    )
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    registry.async_update_entity.assert_not_called()


def test_load_subentry_uses_load_forecast_suffix_not_signal_forecast():
    sub = _make_subentry("xyz789", SUBENTRY_TYPE_LOAD, "sensor.sw24_hws_l3_temperature")
    entry = MagicMock()
    entry.subentries = {"xyz789": sub}
    registry = _make_registry(entity_id_lookup={}, existing_entities=set())
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    called_uid = registry.async_get_entity_id.call_args[0][2]
    assert called_uid == "xyz789_load_forecast", called_uid


def test_non_forecastable_subentry_type_is_skipped_entirely():
    sub = _make_subentry("other1", "some_other_subentry_type", "sensor.irrelevant")
    entry = MagicMock()
    entry.subentries = {"other1": sub}
    registry = _make_registry(entity_id_lookup={}, existing_entities=set())
    registry = _run(hass=MagicMock(), entry=entry, registry=registry)
    registry.async_get_entity_id.assert_not_called()


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
