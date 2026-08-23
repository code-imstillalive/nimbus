"""Real test of flows/power_source_subentry.py -- same reconfigure-vs-new
routing and title-derivation pattern as load_subentry.py/signal_subentry.py
(this file's own docstring points at those siblings for the full "why").

Imports and exercises the REAL methods (not a reimplementation) against
real `voluptuous` and tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import CONF_POWER_SOURCE_NAME  # noqa: E402
from custom_components.nimbus_load.flows.power_source_subentry import (  # noqa: E402
    NimbusPowerSourceSubentryFlowHandler,
)


def _make_flow(source: str = "user") -> NimbusPowerSourceSubentryFlowHandler:
    flow = NimbusPowerSourceSubentryFlowHandler.__new__(
        NimbusPowerSourceSubentryFlowHandler
    )
    flow.source = source
    flow.hass = MagicMock()
    return flow


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry_titled_from_the_name_field():
    flow = _make_flow(source="user")
    result = asyncio.run(
        flow.async_step_user({CONF_POWER_SOURCE_NAME: "Inverter 1 (SH25T)"})
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Inverter 1 (SH25T)"
    assert result["data"] == {CONF_POWER_SOURCE_NAME: "Inverter 1 (SH25T)"}


def test_reconfigure_source_updates_existing_entry_not_creates_new():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_POWER_SOURCE_NAME: "Old Name"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    result = asyncio.run(flow.async_step_user({CONF_POWER_SOURCE_NAME: "New Name"}))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"


def test_reconfigure_with_no_input_prefills_form_from_existing_data():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_POWER_SOURCE_NAME: "Inverter 2 (SH15T)"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    marker = next(
        k for k in result["data_schema"].schema if k == CONF_POWER_SOURCE_NAME
    )
    assert marker.default() == "Inverter 2 (SH15T)"


def test_step_reconfigure_alias_delegates_to_step_user():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_POWER_SOURCE_NAME: "Existing"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    result = asyncio.run(
        flow.async_step_reconfigure({CONF_POWER_SOURCE_NAME: "Existing"})
    )
    assert result["type"] == "update_and_abort"


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
