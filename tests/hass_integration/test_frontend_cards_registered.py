"""Real end-to-end coverage for frontend.py's card registration (nimbus
issue #364, Mark Purcell) -- generalized from a single hardcoded
topology-card registration to a small table of (filename, card type)
pairs, so nimbus-dispatch-card-v4.js ("Control Panel") and
nimbus-regret-card.js ("Regret") ship the same way switchboard-topology-
card.js already did. Nothing exercised this end-to-end before (the
existing unit tests all mock `frontend.async_register_frontend` out
entirely, per that function's own docstring on why a module-level import
would break test collection) -- this is the first test that actually
runs it against a real `hass.http`/`frontend` component, matching the
production path exactly.

Uses `pytest-homeassistant-custom-component`'s real `hass` fixture (via
tests/hass_integration/conftest.py's autouse `http_and_frontend_set_up`)
since a real HTTP static-path registration and a real
`add_extra_js_url()` call are exactly the kind of thing the stub tree
can't model -- same reasoning `test_flap_regression_reload_instance.py`
gives for choosing this harness.
"""

from __future__ import annotations

import pytest
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.frontend import _CARDS

# `enable_custom_integrations` is applied automatically via
# tests/hass_integration/conftest.py's autouse fixture.


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (frontend cards)", data={}, options={}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_every_shipped_card_is_registered_as_an_extra_js_module(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """Sanity check the table itself found real content -- a silent
    empty _CARDS would make every assertion below vacuously pass."""
    assert len(_CARDS) >= 3

    registered_urls = hass.data.get(DATA_EXTRA_MODULE_URL, frozenset())
    for card in _CARDS:
        matches = [
            url
            for url in registered_urls
            if url.startswith(f"/{DOMAIN}/{card.filename}?v=")
        ]
        assert matches, (
            f"{card.card_type!r} ({card.filename}) was not registered as an "
            f"extra JS module -- registered URLs were: {sorted(registered_urls)}"
        )


async def test_every_shipped_card_is_actually_served_over_http(
    hass: HomeAssistant, hass_client, nimbus_entry: MockConfigEntry
):
    """The registered URL must actually resolve to the real file's own
    content -- registering a static path with a typo'd filename would
    pass the extra-js-module check above while 404ing in a real browser."""
    client = await hass_client()
    for card in _CARDS:
        resp = await client.get(f"/{DOMAIN}/{card.filename}")
        assert resp.status == 200, (
            f"{card.filename} did not serve (status {resp.status})"
        )
        body = await resp.text()
        assert card.card_type in body, (
            f"{card.filename}'s served content doesn't mention its own "
            f"card_type {card.card_type!r} -- wrong file served?"
        )
