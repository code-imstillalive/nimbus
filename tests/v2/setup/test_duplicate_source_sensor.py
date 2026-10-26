"""Two subentries on the same source sensor are refused, since their forecast
entities would collide (#362) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
)
from custom_components.nimbus_load.flows.load_subentry import (
    NimbusLoadSubentryFlowHandler,
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


# --- load_subentry.py integration -------------------------------------------


def _make_load_flow(source: str = "user") -> NimbusLoadSubentryFlowHandler:
    flow = NimbusLoadSubentryFlowHandler.__new__(NimbusLoadSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    flow.hass.states.get.return_value = None
    return flow


# --- signal_subentry.py integration -----------------------------------------


def _make_signal_flow(source: str = "user") -> NimbusSignalSubentryFlowHandler:
    flow = NimbusSignalSubentryFlowHandler.__new__(NimbusSignalSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    flow.hass.states.get.return_value = None
    return flow


def test_signal_flow_allows_a_genuinely_new_source_sensor():
    user_input = {CONF_LOAD_SENSOR: "sensor.brand_new"}
    signal_schema({})(user_input)
    flow = _make_signal_flow()
    flow._get_entry = MagicMock(return_value=_entry_with_subentries())
    result = asyncio.run(flow.async_step_user(user_input))
    assert result["type"] == "create_entry"
