"""nimbus #1574, DC-005 step 5a on real Home Assistant: discovery, the
confirmation and the validated source switch.

What this pins:
- discovery writes nothing and calls no service (DC-R13, DC-R15);
- a provider is found by its unique_id, so a renamed entity is still found;
- confirming is refused in legacy, on a moved revision and for anything
  discovery did not offer, each time leaving the store as it was;
- a switch to a source DC-002 cannot read (no entity, a refused unit) is
  refused and changes nothing; a readable one is committed as a new revision,
  confirmed, and today's settings are untouched in shadow.
Synthetic settings only (household: "100% safe").
"""

from __future__ import annotations

import copy
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load import device_contract_ha, device_storage
from custom_components.nimbus_load.const import DOMAIN

OPTIONS = {
    "grid_sensor": "sensor.meter_power",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
}
PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_in_kwh",
            "stat_rate": "sensor.meter_power",
            "entity_energy_price": "sensor.ed_buy",
        }
    ]
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.currency = "AUD"
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (1574 discovery)", data={}, options=dict(OPTIONS)
    )
    entry.add_to_hass(hass)
    return entry


def _prefs(prefs):
    return patch.object(
        device_contract_ha, "_async_energy_prefs", return_value=copy.deepcopy(prefs)
    )


def _stored(hass_storage, entry):
    return hass_storage.get(device_storage.store_key(entry.entry_id), {}).get("data")


async def test_confirming_in_legacy_is_refused(hass: HomeAssistant, hass_storage):
    entry = await _setup(hass)
    with _prefs(PREFS):
        out = await device_contract_ha.async_confirm(
            hass,
            entry,
            0,
            [{"kind": "fill", "device": "grid", "role": "import_energy"}],
        )
    assert out == {"ok": False, "code": "choose_shadow_first", "mode": "legacy"}
    assert _stored(hass_storage, entry) is None

    # test_the_proposal_needs_no_entry
    # DC-007 slice 1 (#1574): the new-install setup shows the proposal before
    # the hub exists. Without a stored document, the service's proposal is
    # exactly the entry-free one, and making it writes nothing.
    with _prefs(PREFS):
        proposal, regs = await device_contract_ha.async_propose(hass)
        out = await device_contract_ha.async_discover(hass, entry)
    assert out["document"] == proposal.document
    assert out["questions"] == proposal.questions
    assert {d["id"] for d in proposal.document["devices"]} >= {"grid"}
    assert isinstance(regs, dict)
    assert _stored(hass_storage, entry) is None


async def _shadow(hass, entry):
    result = await device_contract_ha.async_set_mode(hass, entry, "shadow")
    assert result["ok"] is True, result
    return result["revision"]


async def test_a_moved_revision_or_an_unoffered_choice_changes_nothing(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    revision = await _shadow(hass, entry)
    before = copy.deepcopy(_stored(hass_storage, entry))
    with _prefs(PREFS):
        moved = await device_contract_ha.async_confirm(
            hass, entry, revision + 5, [{"kind": "fill", "device": "grid"}]
        )
        assert moved["code"] == "revision_conflict"
        unoffered = await device_contract_ha.async_confirm(
            hass,
            entry,
            revision,
            [
                {
                    "kind": "fill",
                    "device": "grid",
                    "role": "export_energy",
                    "entity_id": "sensor.anything",
                }
            ],
        )
    assert unoffered["code"] == "choice_refused"
    assert unoffered["refused"][0]["reason"] == "not_offered"
    assert _stored(hass_storage, entry) == before


async def test_rediscovery_offers_the_energy_and_an_alternative_price(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    await _shadow(hass, entry)
    with _prefs(PREFS):
        out = await device_contract_ha.async_discover(hass, entry)
    kinds = {(d["kind"], d.get("role")) for d in out["differences"]}
    assert ("fill", "import_energy") in kinds
    assert ("alternative", "import_price") in kinds
    # The stored grid power is the Energy Dashboard's own power sensor.
    assert ("alternative", "power") not in kinds


async def test_a_switch_to_a_price_in_kilowatts_is_refused(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    revision = await _shadow(hass, entry)
    hass.states.async_set("sensor.ed_buy", "0.31", {"unit_of_measurement": "kW"})
    choice = {
        "kind": "alternative",
        "device": "grid",
        "role": "import_price",
        "entity_id": "sensor.ed_buy",
    }
    with _prefs(PREFS):
        out = await device_contract_ha.async_confirm(hass, entry, revision, [choice])
    assert out["code"] == "binding_refused"
    assert out["refused"][0]["reason"] == "unit_not_price"
    assert _stored(hass_storage, entry)["revision"] == revision


async def test_a_readable_switch_commits_a_confirmed_revision_in_shadow(
    hass: HomeAssistant, hass_storage
):
    entry = await _setup(hass)
    revision = await _shadow(hass, entry)
    hass.states.async_set("sensor.ed_buy", "0.31", {"unit_of_measurement": "AUD/kWh"})
    choices = [
        {
            "kind": "alternative",
            "device": "grid",
            "role": "import_price",
            "entity_id": "sensor.ed_buy",
        },
        {
            "kind": "fill",
            "device": "grid",
            "role": "import_energy",
            "entity_id": "sensor.grid_in_kwh",
        },
    ]
    with (
        _prefs(PREFS),
        # The recorder is not loaded here: a statistic binding is flagged as
        # not validated, never refused on that account.
        patch.object(device_contract_ha, "_async_statistics", return_value=None),
        patch("homeassistant.core.ServiceRegistry.async_call") as calls,
    ):
        out = await device_contract_ha.async_confirm(hass, entry, revision, choices)
    assert calls.call_count == 0
    assert out["ok"] is True, out
    assert out["revision"] == revision + 1
    assert {(c["device"], c["role"]) for c in out["changed"]} == {
        ("grid", "import_price"),
        ("grid", "import_energy"),
    }
    assert [f["reason"] for f in out["flags"]] == ["statistics_not_readable"]
    doc = _stored(hass_storage, entry)["document"]
    grid = next(d for d in doc["devices"] if d["id"] == "grid")
    assert grid["bindings"]["import_price"]["source"]["entity_id"] == "sensor.ed_buy"
    assert grid["bindings"]["import_price"]["provenance"]["confirmed"] is True
    assert grid["bindings"]["import_energy"]["provenance"]["confirmed"] is True
    # Shadow: today's settings stay master.
    assert dict(entry.options) == OPTIONS
