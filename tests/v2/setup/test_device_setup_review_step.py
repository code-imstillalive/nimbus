"""Configure -> Device setup -> Review what Nimbus finds: fills, renames and new devices come pre-ticked,
an alternative never does; what is ticked is confirmed against the revision it was found on, and a
refusal is shown, not swallowed (#2393: flows/device_setup.py's coverage, one behaviour per test).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import device_contract_ha
from custom_components.nimbus_load.flows.device_setup import DeviceSetupSteps

FOUND = {
    "revision": 7,
    "differences": [
        {
            "kind": "fill",
            "device": "battery_1",
            "role": "state_of_charge",
            "binding": {"source": {"entity_id": "sensor.soc"}},
        },
        {
            "kind": "alternative",
            "device": "site",
            "role": "grid_power",
            "candidate": {"source": "sensor.meter_b"},
        },
        {
            "kind": "new_device",
            "device": {"id": "battery_2", "kind": "battery", "name": "Battery 2"},
        },
        {"kind": "conflict", "device": "site", "role": "x"},  # not offered on Review
    ],
}


class _Flow(DeviceSetupSteps):
    def __init__(self) -> None:
        self.hass = MagicMock()
        self.config_entry = MagicMock(options={"kept": True})
        self.async_show_form = MagicMock(side_effect=lambda **kw: kw)
        self.async_create_entry = MagicMock(
            side_effect=lambda **kw: {"type": "create_entry", **kw}
        )


def _review(confirm_result=None, found=FOUND):
    flow = _Flow()
    confirm = AsyncMock(return_value=confirm_result or {"ok": True})
    discover = AsyncMock(
        side_effect=lambda *_a: {**found, "differences": list(found["differences"])}
    )
    with (
        patch.object(device_contract_ha, "async_discover", discover),
        patch.object(device_contract_ha, "async_confirm", confirm),
    ):
        form = asyncio.run(flow.async_step_device_setup_review(None))
        return flow, form, confirm, discover


def _schema(form):
    return {str(k): (k, v) for k, v in form["data_schema"].schema.items()}


def test_review_pre_ticks_fills_renames_and_new_devices_never_an_alternative():
    # test_review_pre_ticks_fills_renames_and_new_devices_never_an_alternative
    _flow, form, _confirm, _discover = _review()
    fields = _schema(form)
    marker, select = fields["choices"]
    labels = [o["label"] for o in select.config["options"]]
    assert labels == [
        "Fill battery_1 state of charge: sensor.soc",
        "Alternative for site grid power: sensor.meter_b (yours stays unless you tick this)",
        "New device: Battery 2",
    ]
    assert marker.default() == [
        "0",
        "2",
    ]  # the alternative (1) waits for the household's own tick
    assert "capacity_kwh" in fields  # a new battery asks for its capacity


def test_review_confirms_exactly_what_is_ticked_against_the_revision_it_found():
    # test_review_confirms_exactly_what_is_ticked_against_the_revision_it_found
    flow, _form, confirm, discover = _review()
    with (
        patch.object(device_contract_ha, "async_discover", discover),
        patch.object(device_contract_ha, "async_confirm", confirm),
    ):
        done = asyncio.run(
            flow.async_step_device_setup_review(
                {"choices": ["2", "1"], "capacity_kwh": 13.5}
            )
        )
    assert done["type"] == "create_entry" and done["data"] == {"kept": True}
    revision, choices = confirm.call_args.args[2], confirm.call_args.args[3]
    assert revision == 7
    assert choices == [
        {
            "kind": "new_device",
            "device": "battery_2",
            "capacity": {"value": 13.5, "unit": "kWh", "basis": "usable"},
        },
        {
            "kind": "alternative",
            "device": "site",
            "role": "grid_power",
            "entity_id": "sensor.meter_b",
        },
    ]


def test_review_shows_a_refusal_and_finishes_untouched_when_nothing_is_ticked():
    # test_review_shows_a_refusal_and_finishes_untouched_when_nothing_is_ticked
    flow, _form, confirm, discover = _review(
        confirm_result={"ok": False, "code": "stale_revision"}
    )
    with (
        patch.object(device_contract_ha, "async_discover", discover),
        patch.object(device_contract_ha, "async_confirm", confirm),
    ):
        refused = asyncio.run(flow.async_step_device_setup_review({"choices": ["0"]}))
        assert refused["errors"] == {
            "base": "stale_revision"
        }  # shown again, with the reason
        untouched = asyncio.run(flow.async_step_device_setup_review({"choices": []}))
    assert untouched["type"] == "create_entry"
    assert confirm.await_count == 1  # an empty tick list confirms nothing


def test_review_with_nothing_new_says_so():
    # test_review_with_nothing_new_says_so
    _flow, form, _confirm, _discover = _review(
        found={"revision": 3, "differences": [{"kind": "conflict"}]}
    )
    assert form["step_id"] == "device_setup_nothing"
