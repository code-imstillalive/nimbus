"""nimbus #1998 (Mark Purcell's review of #1997): since #1994 the Forecaster
knows which load is the whole house only from the whole-house cross-check
field; the fallback to one install's own entity name is gone. An install with
a load forecast and that field blank silently loses the whole-house fixes
(#6 midnight spike, #7 stair-step), so it now gets a Repair that names the
field and clears once it is set."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_health as sh
from custom_components.nimbus_load.const import (
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


def _kinds(options, *, has_load_forecast):
    issues = sh.evaluate_health(
        options=options,
        forecasts=[],
        hub_up_for=timedelta(0),
        has_load_forecast=has_load_forecast,
    )
    return [i.kind for i in issues]


def test_a_load_forecast_without_a_whole_house_sensor_is_a_repair():
    assert sh.KIND_WHOLE_HOUSE_UNSET in _kinds({}, has_load_forecast=True)


def test_it_clears_once_the_field_is_set():
    options = {CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: "sensor.house_power"}
    assert sh.KIND_WHOLE_HOUSE_UNSET not in _kinds(options, has_load_forecast=True)


def test_no_load_forecast_no_repair():
    """A hub with only Power Signals has no whole-house load to anchor."""
    assert sh.KIND_WHOLE_HOUSE_UNSET not in _kinds({}, has_load_forecast=False)


class _FakeIR:
    class IssueSeverity:
        WARNING = "warning"

    def __init__(self):
        self.issues = {}

    def async_create_issue(self, hass, domain, issue_id, **kw):
        self.issues[issue_id] = kw

    def async_delete_issue(self, hass, domain, issue_id):
        self.issues.pop(issue_id, None)


def _refresh(subentry_types, options):
    """The Repairs one refresh leaves, through a fake issue registry (as
    test_1574 does: the HA stub has none)."""
    entry = SimpleNamespace(
        entry_id="E1",
        options=options,
        subentries={
            f"s{i}": SimpleNamespace(subentry_type=t, title=f"s{i}")
            for i, t in enumerate(subentry_types)
        },
        runtime_data={},
    )
    hass = SimpleNamespace(data={})
    fake = _FakeIR()
    helpers = types.ModuleType("homeassistant.helpers")
    with (
        patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": fake}),
        patch.object(
            sys.modules.get("homeassistant.helpers", helpers),
            "issue_registry",
            fake,
            create=True,
        ),
        patch(
            "custom_components.nimbus_load.power_input_check.find_energy_unit_inputs",
            return_value=[],
        ),
        patch(
            "custom_components.nimbus_load.power_input_check.find_non_power_unit_inputs",
            return_value=[],
        ),
        patch(
            "custom_components.nimbus_load.price_input_check.find_frozen_price_inputs",
            return_value=[],
        ),
        patch(
            "custom_components.nimbus_load.pricing_autodetect.detect_fees_doubled",
            return_value=None,
        ),
        patch(
            "custom_components.nimbus_load.pricing_autodetect.detect_fees_on_pd7day_tariff",
            return_value=None,
        ),
        patch(
            "custom_components.nimbus_load.pricing_autodetect.detect_missing_regional_forecast",
            return_value=None,
        ),
        patch.object(sh, "_live_settings", return_value={}),
    ):
        asyncio.run(sh.async_refresh_health(hass, entry))
    return list(fake.issues)


def test_the_refresh_finds_a_load_subentry_and_only_a_load():
    issue_id = f"E1_{sh.KIND_WHOLE_HOUSE_UNSET}"
    assert issue_id in _refresh([SUBENTRY_TYPE_LOAD], {})
    assert issue_id not in _refresh([SUBENTRY_TYPE_SIGNAL], {})


def test_it_has_its_text_and_the_field_says_what_it_is_for():
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((ROOT / name).read_text(encoding="utf-8"))
        issue = text["issues"][sh.KIND_WHOLE_HOUSE_UNSET]
        assert "whole-house" in issue["title"]
        assert "clears itself" in issue["description"]
        about = text["options"]["step"]["solver_sources"]["data_description"][
            CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
        ]
        assert "which load is the whole house" in about
