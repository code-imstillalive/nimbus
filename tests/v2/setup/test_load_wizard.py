"""The load wizard: no None default reaches the frontend, and reconfigure or
new (#1837).
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
import voluptuous as vol

from custom_components.nimbus_load.const import (
    CONF_EXPECTED_LOAD_KW,
    CONF_LOAD_SENSOR,
    CONF_SCHEDULE_START_HOUR,
)
from custom_components.nimbus_load.flows.load_subentry import (
    NimbusLoadSubentryFlowHandler,
    _schema,
)


def _find_marker(schema: vol.Schema, key: str):
    return next(k for k in schema.schema if k == key)


def _make_flow(source: str = "user") -> NimbusLoadSubentryFlowHandler:
    flow = NimbusLoadSubentryFlowHandler.__new__(NimbusLoadSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    # nimbus issue #362 finding 4d: _async_step() now calls self._get_entry()
    # on every real submission (the duplicate-source-sensor collision check)
    # -- defaults to a real, empty subentries dict (no collision) so every
    # existing test not specifically exercising that check keeps working
    # unchanged. A test that DOES want to exercise it overrides this.
    flow._get_entry = MagicMock(return_value=MagicMock(subentries={}))
    return flow


# -- _schema(): the real 2026-08-15 None-default frontend crash guard -------


def test_expected_load_kw_carries_real_default_once_configured():
    schema = _schema({CONF_EXPECTED_LOAD_KW: 3.7})
    marker = _find_marker(schema, CONF_EXPECTED_LOAD_KW)
    assert marker.default() == 3.7


# -- _derive_title: friendly_name preferred, entity_id as fallback ----------


# -- async_step_user: real, live-bitten reconfigure-vs-new routing ----------


def test_reconfigure_with_no_input_shows_form_prefilled_from_existing_data():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(
        data={CONF_LOAD_SENSOR: "sensor.existing", CONF_SCHEDULE_START_HOUR: 11.0}
    )
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    marker = _find_marker(result["data_schema"], CONF_SCHEDULE_START_HOUR)
    assert marker.default() == 11.0  # prefilled from the existing subentry, not blank
