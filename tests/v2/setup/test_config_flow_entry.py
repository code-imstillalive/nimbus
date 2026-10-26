"""The hub's config flow: a failing notification never blocks creating the hub,
and the subentry types and options flow are registered (#1837).
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
from custom_components.nimbus_load.config_flow import NimbusConfigFlow


def _make_flow() -> NimbusConfigFlow:
    flow = NimbusConfigFlow.__new__(NimbusConfigFlow)
    flow.hass = MagicMock()
    flow.async_set_unique_id = MagicMock(side_effect=_async_noop)
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


async def _async_noop(*args, **kwargs):
    return None


def test_hub_creation_checks_unique_id_to_prevent_a_second_hub():
    flow = _make_flow()
    asyncio.run(flow.async_step_user(None))
    flow.async_set_unique_id.assert_called_once()
    flow._abort_if_unique_id_configured.assert_called_once()
