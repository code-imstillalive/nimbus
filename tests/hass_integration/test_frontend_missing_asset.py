"""An install with its card files missing (HACS did not copy frontend/) logs each one and registers
nothing, leaving no flag behind, so the next start after a fixed install registers normally
(#2393: frontend.py's coverage, one behaviour).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.nimbus_load import frontend
from custom_components.nimbus_load.const import DOMAIN


async def test_missing_card_files_are_reported_and_nothing_is_registered(
    hass: HomeAssistant, tmp_path, caplog
):
    caplog.set_level(logging.ERROR, logger="custom_components.nimbus_load.frontend")
    with (
        patch.object(frontend, "_FRONTEND_DIR", tmp_path),
        patch("homeassistant.components.frontend.add_extra_js_url") as add_js,
    ):
        await frontend.async_register_frontend(hass, "0.0.0")
    missing = [r for r in caplog.records if "asset missing" in r.getMessage()]
    assert len(missing) == len(frontend._CARDS)
    add_js.assert_not_called()
    assert not any(k.startswith(f"{DOMAIN}_frontend_registered_") for k in hass.data)
