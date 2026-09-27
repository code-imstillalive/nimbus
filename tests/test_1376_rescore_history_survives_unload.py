"""nimbus issue #1376 (IV&V pass #1375): async_unregister_services() removes
five of the six services async_register_services() registers -- rescore_history
is missing from the unregister tuple, so it stays registered and callable
forever after a genuine unload/removal, the exact #365 failure class this same
function's own docstring cites as the reason it exists at all.

tests/test_services.py already has a test pinning the CURRENT (buggy)
asymmetry (test_async_unregister_services_removes_all_three) with a docstring
noting the gap and claiming "#495's own PR filed it as its own issue" -- that
claim did not hold up (no such issue existed until #1376). This file pins the
CORRECT behaviour instead: every service HA actually has registered should be
removed on unload. It is deliberately xfail(strict=True) -- it must fail
against current code for exactly the reason above, and flips to a loud XPASS
the moment #1376 is fixed, which is the signal to drop the marker.

Imports and exercises the REAL module, same pattern as test_services.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import services


@pytest.mark.xfail(
    reason="nimbus issue #1376: rescore_history is registered by "
    "async_register_services() but missing from async_unregister_services()'s "
    "own tuple, so it survives a genuine unload/removal indefinitely.",
    strict=True,
)
def test_unregister_removes_every_service_that_register_registers():
    hass = MagicMock()
    hass.services.has_service.return_value = False
    services.async_register_services(hass)
    registered = {call.args[1] for call in hass.services.async_register.call_args_list}

    hass2 = MagicMock()
    hass2.services.has_service.return_value = True
    services.async_unregister_services(hass2)
    removed = {call.args[1] for call in hass2.services.async_remove.call_args_list}

    assert removed == registered, (
        f"async_unregister_services() left {registered - removed} registered "
        f"after unload -- every service async_register_services() can "
        f"register must be removable, per this function's own #365 docstring."
    )


def test_rescore_history_is_the_one_survivor_today():
    """Non-vacuity / diagnostic companion: names the exact gap rather than
    leaving the strict xfail above as the only evidence. Not itself an
    xfail -- this passes today and should keep passing after #1376 is fixed
    (the difference becomes the empty set, which is still a valid, if
    less interesting, assertion of the same shape)."""
    hass = MagicMock()
    hass.services.has_service.return_value = False
    services.async_register_services(hass)
    registered = {call.args[1] for call in hass.services.async_register.call_args_list}

    hass2 = MagicMock()
    hass2.services.has_service.return_value = True
    services.async_unregister_services(hass2)
    removed = {call.args[1] for call in hass2.services.async_remove.call_args_list}

    survivors = registered - removed
    assert survivors in ({services.SERVICE_RESCORE_HISTORY}, set()), (
        f"expected rescore_history to be the one known survivor (or none, "
        f"once #1376 is fixed) -- got {survivors}, a different/new gap."
    )
