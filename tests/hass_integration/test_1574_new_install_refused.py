"""nimbus #1574 DC-007 slice 1 on real Home Assistant: a confirmed site the
first setup cannot save.

A hub made by the new-install setup carries the site the household
confirmed; its first setup commits it, then the entry forgets it. When the
commit is refused, or fails outright, the hub stays as it was before the
flow, the pending site is still forgotten (so it is not retried on every
start), and a Repair says why. Main's CI never ran this path (#2393). Only
the commit is patched. Synthetic only (household: "100% safe")."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha
from custom_components.nimbus_load.const import DOMAIN

SITE = {"schema_version": 1, "devices": []}


@pytest.mark.parametrize(
    ("adopt", "reason"),
    [
        (
            AsyncMock(return_value={"ok": False, "code": "invalid_document"}),
            "invalid_document",
        ),
        (AsyncMock(side_effect=RuntimeError("store unwritable")), "error"),
    ],
    ids=["refused", "failed"],
)
async def test_a_site_that_cannot_be_saved_is_forgotten_and_a_repair_says_why(
    hass: HomeAssistant, adopt, reason
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus",
        data={"pending_device_document": SITE, "kept": 1},
        options={},
    )
    entry.add_to_hass(hass)
    with patch.object(device_contract_ha, "async_adopt_new_install", adopt):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    adopt.assert_awaited_once()
    assert entry.data == {"kept": 1}
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"new_install_setup_failed_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {"reason": reason}


async def test_a_confirmed_site_that_does_not_validate_is_refused_and_nothing_written(
    hass: HomeAssistant,
):
    """#1574 DC-007 slice 1, #2393: the real commit, not a patched one. A site
    the store's validation refuses comes back with its problems and the hub
    keeps no document and stays in `legacy`, as before the flow."""
    entry = MockConfigEntry(domain=DOMAIN, title="Nimbus", data={}, options={})
    entry.add_to_hass(hass)

    out = await device_contract_ha.async_adopt_new_install(hass, entry, SITE)

    assert (out["ok"], out["code"], out["mode"]) == (
        False,
        "document_invalid",
        "legacy",
    )
    assert out["problems"] and all(
        {"code", "path", "message"} <= set(p) for p in out["problems"]
    )
    state = await device_contract_ha._store(hass, entry).async_read()
    assert (state["document"], state["mode"]) == (None, "legacy")
