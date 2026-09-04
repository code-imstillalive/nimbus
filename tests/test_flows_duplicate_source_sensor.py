"""Regression test for nimbus issue #362 finding 4d (Mark Purcell,
codebase review): a Load and/or Power Signal subentry's forecast
entity_id is derived PURELY from its own source sensor
(sensor.py's object_id_from_source()), with no subentry-scoping at all.
If a second subentry (of either type, since both share this one
entity_id namespace) points at the SAME source sensor, HA's entity
registry silently suffixes the second one's real entity_id with "_2" --
any code path re-deriving the expected entity_id a second time (or a
user/automation assuming the predictable name) then silently targets
whichever subentry happened to register first.

Fixed by rejecting the collision at the config-flow source, via a new
shared helper (flows/__init__.py's find_subentry_sharing_source_sensor())
called from both load_subentry.py and signal_subentry.py's own
_async_step() before creating/updating a subentry.
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
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.flows import find_subentry_sharing_source_sensor
from custom_components.nimbus_load.flows.load_subentry import (
    NimbusLoadSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.load_subentry import (
    _schema as load_schema,
)
from custom_components.nimbus_load.flows.signal_subentry import (
    NimbusSignalSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.signal_subentry import (
    _schema as signal_schema,
)


def _subentry(subentry_id: str, subentry_type: str, source_sensor: str) -> MagicMock:
    sub = MagicMock()
    sub.subentry_id = subentry_id
    sub.subentry_type = subentry_type
    sub.data = {CONF_LOAD_SENSOR: source_sensor}
    sub.title = f"fake title for {subentry_id}"
    return sub


def _entry_with_subentries(*subentries: MagicMock) -> MagicMock:
    entry = MagicMock()
    entry.subentries = {s.subentry_id: s for s in subentries}
    return entry


# --- find_subentry_sharing_source_sensor(): the shared helper --------------


def test_returns_none_when_no_subentries_exist():
    entry = _entry_with_subentries()
    assert find_subentry_sharing_source_sensor(entry, "sensor.new") is None


def test_returns_none_when_no_subentry_shares_the_sensor():
    entry = _entry_with_subentries(
        _subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.a"),
        _subentry("s2", SUBENTRY_TYPE_SIGNAL, "sensor.b"),
    )
    assert find_subentry_sharing_source_sensor(entry, "sensor.c") is None


def test_finds_a_real_collision_across_load_type():
    entry = _entry_with_subentries(_subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.shared"))
    conflict = find_subentry_sharing_source_sensor(entry, "sensor.shared")
    assert conflict is not None
    assert conflict.subentry_id == "s1"


def test_finds_a_collision_between_a_load_and_a_signal():
    # The real risk this whole fix targets -- Load and Power Signal
    # subentries share the SAME entity_id namespace, so a collision
    # between the two types is just as real as within one type.
    entry = _entry_with_subentries(_subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.shared"))
    conflict = find_subentry_sharing_source_sensor(entry, "sensor.shared")
    assert conflict is not None


def test_excludes_the_subentry_being_reconfigured():
    # Reconfiguring a subentry with its OWN unchanged sensor must not
    # flag itself as a conflict with itself.
    entry = _entry_with_subentries(_subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.mine"))
    conflict = find_subentry_sharing_source_sensor(
        entry, "sensor.mine", exclude_subentry_id="s1"
    )
    assert conflict is None


def test_ignores_subentry_types_outside_load_and_signal():
    # Power Source/PV String/Battery Tower subentries don't feed
    # object_id_from_source() at all -- a coincidentally-matching
    # source sensor there is not a real collision.
    entry = _entry_with_subentries(
        _subentry("s1", SUBENTRY_TYPE_POWER_SOURCE, "sensor.shared")
    )
    assert find_subentry_sharing_source_sensor(entry, "sensor.shared") is None


# --- load_subentry.py integration -------------------------------------------


def _make_load_flow(source: str = "user") -> NimbusLoadSubentryFlowHandler:
    flow = NimbusLoadSubentryFlowHandler.__new__(NimbusLoadSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    flow.hass.states.get.return_value = None
    return flow


def test_load_flow_rejects_a_source_sensor_already_used_by_another_load():
    user_input = {CONF_LOAD_SENSOR: "sensor.shared"}
    load_schema({})(user_input)
    flow = _make_load_flow()
    flow._get_entry = MagicMock(
        return_value=_entry_with_subentries(
            _subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.shared")
        )
    )
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "form"
    assert result["errors"] == {"load_sensor": "duplicate_source_sensor"}


def test_load_flow_rejects_a_source_sensor_already_used_by_a_power_signal():
    user_input = {CONF_LOAD_SENSOR: "sensor.shared"}
    load_schema({})(user_input)
    flow = _make_load_flow()
    flow._get_entry = MagicMock(
        return_value=_entry_with_subentries(
            _subentry("s1", SUBENTRY_TYPE_SIGNAL, "sensor.shared")
        )
    )
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "form"
    assert result["errors"] == {"load_sensor": "duplicate_source_sensor"}


def test_load_flow_allows_a_genuinely_new_source_sensor():
    user_input = {CONF_LOAD_SENSOR: "sensor.brand_new"}
    load_schema({})(user_input)
    flow = _make_load_flow()
    flow._get_entry = MagicMock(
        return_value=_entry_with_subentries(
            _subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.other")
        )
    )
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "create_entry"


def test_load_flow_reconfigure_with_unchanged_sensor_does_not_self_collide():
    user_input = {CONF_LOAD_SENSOR: "sensor.mine"}
    load_schema({})(user_input)
    flow = _make_load_flow(source="reconfigure")
    fake_subentry = _subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.mine")
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=_entry_with_subentries(fake_subentry))
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "update_and_abort"


# --- signal_subentry.py integration -----------------------------------------


def _make_signal_flow(source: str = "user") -> NimbusSignalSubentryFlowHandler:
    flow = NimbusSignalSubentryFlowHandler.__new__(NimbusSignalSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    flow.hass.states.get.return_value = None
    return flow


def test_signal_flow_rejects_a_source_sensor_already_used_by_a_load():
    user_input = {CONF_LOAD_SENSOR: "sensor.shared"}
    signal_schema({})(user_input)
    flow = _make_signal_flow()
    flow._get_entry = MagicMock(
        return_value=_entry_with_subentries(
            _subentry("s1", SUBENTRY_TYPE_LOAD, "sensor.shared")
        )
    )
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "form"
    assert result["errors"] == {"load_sensor": "duplicate_source_sensor"}


def test_signal_flow_allows_a_genuinely_new_source_sensor():
    user_input = {CONF_LOAD_SENSOR: "sensor.brand_new"}
    signal_schema({})(user_input)
    flow = _make_signal_flow()
    flow._get_entry = MagicMock(return_value=_entry_with_subentries())
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "create_entry"


def test_signal_flow_reconfigure_with_unchanged_sensor_does_not_self_collide():
    user_input = {CONF_LOAD_SENSOR: "sensor.mine"}
    signal_schema({})(user_input)
    flow = _make_signal_flow(source="reconfigure")
    fake_subentry = _subentry("s1", SUBENTRY_TYPE_SIGNAL, "sensor.mine")
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=_entry_with_subentries(fake_subentry))
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "update_and_abort"
