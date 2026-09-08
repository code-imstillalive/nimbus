"""Real end-to-end coverage for frontend.py's card registration (nimbus
issue #364, Mark Purcell) -- generalized from a single hardcoded
topology-card registration to a small table of (filename, card type)
pairs, so nimbus-dispatch-card-v4.js ("Control Panel") and
nimbus-regret-card.js ("Regret") ship the same way nimbus-topology-
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

import re

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

    # HA core stores this as a `frontend.UrlManager` instance (its own
    # `.urls` frozenset attribute), not a bare frozenset directly --
    # confirmed live in CI, not assumed: this module's own docstring on
    # add_extra_js_url() says "stores URLs in a frozenset", which is true
    # of UrlManager.urls but not of hass.data[DATA_EXTRA_MODULE_URL]
    # itself, which is the manager object. `getattr(..., "urls", ...)`
    # falls back to treating the raw value as already-iterable, in case
    # a future/older HA version ever stores a bare frozenset directly.
    url_container = hass.data.get(DATA_EXTRA_MODULE_URL, frozenset())
    registered_urls = getattr(url_container, "urls", url_container)
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


# nimbus issue #519 (Mark Purcell): the topology card's own `type` and
# picker `name` had neither "nimbus" in them ("switchboard-topology-
# card" / "Topology Card"), so a household searching the card picker for
# "nimbus" -- having already found the other two cards that way --
# concluded the third one wasn't deployed. Regex against the served
# content rather than a real JS parser: this project has no JS test
# runner, and the `window.customCards.push({...})` block is a small,
# stable, single-object literal every shipped card already writes the
# same way (confirmed by eye across all three files).
_CUSTOM_CARDS_PUSH_RE = re.compile(
    r"window\.customCards\.push\(\{(?P<body>.*?)\}\);", re.DOTALL
)
_FIELD_RE = re.compile(r"""(\w+):\s*["'](.*?)["']""")


async def test_every_shipped_cards_picker_identity_says_nimbus(
    hass: HomeAssistant, hass_client, nimbus_entry: MockConfigEntry
):
    """So the next new card -- or the next rename -- can't silently drift
    back into a picker entry a household can't find by searching
    "nimbus", the way this issue's own topology card did."""
    client = await hass_client()
    for card in _CARDS:
        resp = await client.get(f"/{DOMAIN}/{card.filename}")
        body = await resp.text()
        match = _CUSTOM_CARDS_PUSH_RE.search(body)
        assert match, (
            f"{card.filename} has no window.customCards.push({{...}}) "
            f"block -- not registered in the card picker at all"
        )
        fields = dict(_FIELD_RE.findall(match.group("body")))
        assert fields.get("type", "").startswith("nimbus-"), (
            f"{card.filename}'s picker type {fields.get('type')!r} doesn't "
            f"start with 'nimbus-' -- not findable by searching \"nimbus\" "
            f"in the card picker"
        )
        assert "nimbus" in fields.get("name", "").lower(), (
            f"{card.filename}'s picker name {fields.get('name')!r} doesn't "
            f'mention Nimbus -- not findable by searching "nimbus" in '
            f"the card picker"
        )
        # The type registered in the picker must also be the one actually
        # served -- i.e. _CARDS' own card_type, not a stale alias.
        assert fields.get("type") == card.card_type, (
            f"{card.filename}'s picker type {fields.get('type')!r} doesn't "
            f"match _CARDS' own registered card_type {card.card_type!r}"
        )


# nimbus issue #551 (Mark Purcell): HA's card picker inserts
# `{ type: "custom:nimbus-topology-card" }` with no other keys when a
# household adds a card from the picker -- with no getStubConfig() on
# any of the three shipped cards, the topology card's own setConfig()
# threw on that bare config, producing a red error card as the very
# first thing a household saw right after #519 made the card findable
# at all. Regex against the SERVED content (not source) for the same
# reason _CUSTOM_CARDS_PUSH_RE above is: no JS test runner in this
# project, and this confirms what a real browser actually receives,
# not just what's in the repo.
_GET_STUB_CONFIG_RE = re.compile(r"static\s+getStubConfig\s*\(")
_TOPOLOGY_OLD_THROW_RE = re.compile(
    r"if\s*\(\s*!config\.switchboard\s*\|\|\s*!config\.inverters\s*\)"
)


async def test_every_shipped_card_defines_a_stub_config(
    hass: HomeAssistant, hass_client, nimbus_entry: MockConfigEntry
):
    """Every shipped card must define static getStubConfig() so HA's own
    card picker never has to fall back to a bare `{ type: ... }` config."""
    client = await hass_client()
    for card in _CARDS:
        resp = await client.get(f"/{DOMAIN}/{card.filename}")
        body = await resp.text()
        assert _GET_STUB_CONFIG_RE.search(body), (
            f"{card.filename} has no static getStubConfig() -- HA's card "
            f"picker will insert a bare config with none of this card's "
            f"fields set"
        )


async def test_topology_card_no_longer_throws_on_a_missing_switchboard_or_inverters(
    hass: HomeAssistant, hass_client, nimbus_entry: MockConfigEntry
):
    """The exact pre-#551 throw condition must be gone from the SERVED
    topology card bundle -- a missing switchboard/inverters key now
    defaults to {}/[] instead of erroring, matching the minimal config
    docs/dashboards.md already tells a household to write by hand."""
    client = await hass_client()
    topology_card = next(c for c in _CARDS if "topology" in c.filename)
    resp = await client.get(f"/{DOMAIN}/{topology_card.filename}")
    body = await resp.text()
    assert not _TOPOLOGY_OLD_THROW_RE.search(body), (
        f"{topology_card.filename} still contains the pre-#551 "
        f"'!config.switchboard || !config.inverters' throw condition -- "
        f"a picker-added card with no config would still error"
    )
