"""Every subentry form says "Edit" when opened from an existing entry's cog
and "Add" for a new one (household, 25 Oct 2026: editing the EV charger
load showed "Add a load")."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from homeassistant.config_entries import SOURCE_RECONFIGURE

from custom_components.nimbus_load.flows import form_action_placeholders
from custom_components.nimbus_load.flows.battery_tower_subentry import (
    NimbusBatteryTowerSubentryFlowHandler,
)

COMPONENT = Path(__file__).resolve().parents[3] / "custom_components" / "nimbus_load"


def _flow(source: str) -> NimbusBatteryTowerSubentryFlowHandler:
    flow = NimbusBatteryTowerSubentryFlowHandler.__new__(
        NimbusBatteryTowerSubentryFlowHandler
    )
    flow.source = source
    flow.hass = MagicMock()
    entry = MagicMock()
    entry.subentries = {}
    flow._get_entry = MagicMock(return_value=entry)
    sub = MagicMock()
    sub.data = {}
    flow._get_reconfigure_subentry = MagicMock(return_value=sub)
    return flow


def test_action_is_edit_on_reconfigure_and_add_otherwise():
    assert form_action_placeholders(_flow(SOURCE_RECONFIGURE)) == {"action": "Edit"}
    assert form_action_placeholders(_flow("user")) == {"action": "Add"}


def test_a_shown_form_carries_the_action():
    new = asyncio.run(_flow("user").async_step_user(None))
    edit = asyncio.run(_flow(SOURCE_RECONFIGURE).async_step_user(None))
    assert new["description_placeholders"] == {"action": "Add"}
    assert edit["description_placeholders"] == {"action": "Edit"}


def test_every_subentry_title_uses_the_action_and_every_form_passes_it():
    for name in ("strings.json", "translations/en.json"):
        subentries = json.loads((COMPONENT / name).read_text(encoding="utf-8"))[
            "config_subentries"
        ]
        for kind, spec in subentries.items():
            title = spec["step"]["user"]["title"]
            assert title.startswith("{action} "), (name, kind, title)
    for flow_file in (COMPONENT / "flows").glob("*_subentry.py"):
        src = flow_file.read_text(encoding="utf-8")
        assert src.count("async_show_form(") == src.count(
            "description_placeholders=form_action_placeholders(self)"
        ), flow_file.name
