"""Real test of config_flow.py's NimbusConfigFlow -- the real, deliberate
"a failing notification must never block real hub creation" guarantee
(config_flow.py's own comment: "Wrapped -- hub creation must succeed
regardless of whether this notification does"), plus the subentry-type
registration and options-flow wiring.

Imports and exercises the REAL class (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules.
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
from custom_components.nimbus_load.config_flow import NimbusConfigFlow  # noqa: E402
from custom_components.nimbus_load.const import (  # noqa: E402
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.flows.battery_tower_subentry import NimbusBatteryTowerSubentryFlowHandler  # noqa: E402
from custom_components.nimbus_load.flows.hub_options import NimbusHubOptionsFlow  # noqa: E402
from custom_components.nimbus_load.flows.load_subentry import (  # noqa: E402
    NimbusLoadSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.power_source_subentry import (  # noqa: E402
    NimbusPowerSourceSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.pv_string_subentry import (  # noqa: E402
    NimbusPvStringSubentryFlowHandler,
)
from custom_components.nimbus_load.flows.signal_subentry import (  # noqa: E402
    NimbusSignalSubentryFlowHandler,
)


def _make_flow() -> NimbusConfigFlow:
    flow = NimbusConfigFlow.__new__(NimbusConfigFlow)
    flow.hass = MagicMock()
    flow.async_set_unique_id = MagicMock(side_effect=_async_noop)
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


async def _async_noop(*args, **kwargs):
    return None


def test_hub_creation_succeeds_and_calls_the_real_setup_notification():
    flow = _make_flow()
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "create_entry"
    assert result["title"] == "Nimbus"
    assert result["data"] == {}
    flow.hass.services.async_call.assert_called_once()
    call_args = flow.hass.services.async_call.call_args
    assert call_args[0][0] == "persistent_notification"
    assert call_args[0][1] == "create"
    assert call_args[0][2]["notification_id"] == "nimbus_setup_incomplete"


def test_hub_creation_still_succeeds_even_if_the_notification_call_raises():
    # The real, deliberate guarantee this exists to protect (config_flow.py's
    # own comment: "hub creation must succeed regardless of whether this
    # notification does"). A persistent_notification failure is a real,
    # plausible scenario (hass not fully started yet, a services registry
    # hiccup) -- it must never prevent the actual hub entry from being
    # created.
    flow = _make_flow()
    flow.hass.services.async_call = MagicMock(
        side_effect=RuntimeError("services not ready")
    )
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "create_entry"
    assert result["title"] == "Nimbus"


def test_hub_creation_checks_unique_id_to_prevent_a_second_hub():
    flow = _make_flow()
    asyncio.run(flow.async_step_user(None))
    flow.async_set_unique_id.assert_called_once()
    flow._abort_if_unique_id_configured.assert_called_once()


def test_supported_subentry_types_registers_every_type():
    result = NimbusConfigFlow.async_get_supported_subentry_types(MagicMock())
    assert result == {
        SUBENTRY_TYPE_LOAD: NimbusLoadSubentryFlowHandler,
        SUBENTRY_TYPE_SIGNAL: NimbusSignalSubentryFlowHandler,
        SUBENTRY_TYPE_POWER_SOURCE: NimbusPowerSourceSubentryFlowHandler,
        SUBENTRY_TYPE_PV_STRING: NimbusPvStringSubentryFlowHandler,
        SUBENTRY_TYPE_BATTERY_TOWER: NimbusBatteryTowerSubentryFlowHandler,
    }


def test_options_flow_wraps_the_real_config_entry():
    fake_entry = MagicMock()
    result = NimbusConfigFlow.async_get_options_flow(fake_entry)
    assert isinstance(result, NimbusHubOptionsFlow)
    assert result.config_entry is fake_entry


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
