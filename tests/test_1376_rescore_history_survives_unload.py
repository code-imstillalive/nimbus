"""nimbus issue #1376 (IV&V pass #1375): async_unregister_services() used to
remove five of the six services async_register_services() registers --
rescore_history was missing from the unregister tuple, so it survived a
genuine unload/removal forever, the exact #365 failure class this same
function's own docstring cites as the reason it exists at all.

Fixed in PR #1378 (SERVICE_RESCORE_HISTORY added to the tuple, plus a new
AST-derived tests/test_services_register_unregister_symmetry.py covering the
same property from the source side). This file's own
test_unregister_removes_every_service_that_register_registers was originally
xfail(strict=True) -- confirmed to fail for the documented reason against
pre-#1378 code, then confirmed to XPASS once #1378 landed (proving the fix
real, not assumed), at which point the marker was dropped per its own stated
plan. Kept as a permanent regression guard rather than deleted: it checks a
different thing than the AST-derived test (real call-tracking through a
MagicMock hass, not source-text symmetry), which is why #1378's own author
asked for both to stay.

Imports and exercises the REAL module, same pattern as test_services.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import services


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
    """Non-vacuity / diagnostic companion, written before #1378's fix landed
    to tolerate either state so it never needed editing across the fix: it
    named the exact expected gap while #1376 was open, and keeps passing now
    that #1378 has closed it (the difference is the empty set)."""
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
        f"now that #1376/#1378 are resolved) -- got {survivors}, a "
        f"different/new gap."
    )
