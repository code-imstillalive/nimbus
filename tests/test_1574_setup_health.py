"""nimbus #1574 stage 2: setup gaps as Home Assistant Repairs that clear
themselves (design docs/design/two-step-setup.md §7)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_health as sh

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
FULL_SOLVER = {
    "solver_battery_soc_sensor": "sensor.soc",
    "solver_load_forecast_sensor": "sensor.load_fc",
    "solver_solar_forecast_sensor": "sensor.pv_fc",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
}
UP = sh.TRAIN_GRACE + timedelta(minutes=1)


def _kinds(issues):
    return sorted(i.kind for i in issues)


# --- evaluate_health ------------------------------------------------------------


def test_a_healthy_install_has_no_issues():
    issues = sh.evaluate_health(
        options=FULL_SOLVER,
        forecasts=[("a", "Whole House", {"trained_at": "2026-10-06T03:00:00"})],
        hub_up_for=UP,
    )
    assert issues == []


def test_an_untrained_forecast_is_reported_with_its_reason():
    issues = sh.evaluate_health(
        options=FULL_SOLVER,
        forecasts=[
            (
                "a",
                "Hot Water",
                {
                    "trained_at": None,
                    "forecast": [],
                    "last_retrain_error": "Only 12 usable training points",
                },
            ),
            ("b", "Oven", {"trained_at": None, "forecast": []}),
        ],
        hub_up_for=UP,
    )
    by = {i.placeholders["name"]: i.placeholders["reason"] for i in issues}
    assert by == {
        "Hot Water": "Only 12 usable training points",
        "Oven": "it has not found enough usable history yet",
    }
    assert {i.key for i in issues} == {
        "forecast_not_trained_a",
        "forecast_not_trained_b",
    }


def test_no_untrained_report_during_the_startup_grace():
    issues = sh.evaluate_health(
        options=FULL_SOLVER,
        forecasts=[("a", "Oven", {"trained_at": None})],
        hub_up_for=sh.TRAIN_GRACE - timedelta(minutes=1),
    )
    assert issues == []


def test_a_deterministic_load_with_a_forecast_is_not_untrained():
    """#1575: a schedule + expected-kW load publishes a forecast with no model."""
    issues = sh.evaluate_health(
        options=FULL_SOLVER,
        forecasts=[
            (
                "p",
                "Pool Pump",
                {"trained_at": None, "forecast": [{"time": "t", "value": 1.3}]},
            )
        ],
        hub_up_for=UP,
    )
    assert issues == []


def test_missing_solver_inputs_are_named():
    issues = sh.evaluate_health(
        options={
            **FULL_SOLVER,
            "solver_battery_soc_sensor": None,
            "solver_export_price_sensor": "",
        },
        forecasts=[],
        hub_up_for=UP,
    )
    assert _kinds(issues) == [sh.KIND_SOLVER_INPUT]
    assert issues[0].placeholders["inputs"] == "battery state of charge, export price"


def test_energy_inputs_and_doubled_fees_become_issues():
    issues = sh.evaluate_health(
        options=FULL_SOLVER,
        forecasts=[],
        hub_up_for=UP,
        energy_unit_inputs=[("Forecaster: solar sensor", "sensor.solar_total", "Wh")],
        fees_doubled=(
            "sensor.localvolts_v2_buy_flex_up",
            "`solver_flat_fee_rate` = 0.008246",
        ),
    )
    assert _kinds(issues) == sorted([sh.KIND_ENERGY_UNIT, sh.KIND_FEES_DOUBLED])
    energy = next(i for i in issues if i.kind == sh.KIND_ENERGY_UNIT)
    assert "sensor.solar_total" in energy.placeholders["inputs"]


# --- every issue has its translation, with the placeholders it uses --------------


def test_every_kind_is_translated_in_both_files_with_matching_placeholders():
    import re

    for rel in ("strings.json", "translations/en.json"):
        issues = json.loads((ROOT / rel).read_text(encoding="utf-8"))["issues"]
        for kind, placeholders in (
            (sh.KIND_NOT_TRAINED, {"name", "reason"}),
            (sh.KIND_ENERGY_UNIT, {"inputs"}),
            (sh.KIND_FEES_DOUBLED, {"flex_up", "fees"}),
            (sh.KIND_SOLVER_INPUT, {"inputs"}),
        ):
            text = issues[kind]["title"] + issues[kind]["description"]
            assert set(re.findall(r"\{(\w+)\}", text)) <= placeholders, (rel, kind)


# --- async_refresh_health: creates and clears Repairs -----------------------------


class _FakeIR:
    class IssueSeverity:
        WARNING = "warning"

    def __init__(self):
        self.issues = {}

    def async_create_issue(self, hass, domain, issue_id, **kw):
        self.issues[issue_id] = kw

    def async_delete_issue(self, hass, domain, issue_id):
        self.issues.pop(issue_id, None)


def _entry(options, coordinators):
    return SimpleNamespace(
        entry_id="e1",
        options=options,
        subentries={k: SimpleNamespace(title=t) for k, (t, _) in coordinators.items()},
        runtime_data={k: SimpleNamespace(data=d) for k, (_, d) in coordinators.items()},
    )


def test_refresh_creates_then_clears_a_repair():
    fake = _FakeIR()
    hass = SimpleNamespace(data={})
    start = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
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
            "custom_components.nimbus_load.pricing_autodetect.detect_fees_doubled",
            return_value=None,
        ),
    ):
        untrained = _entry(FULL_SOLVER, {"a": ("Oven", {"trained_at": None})})
        asyncio.run(
            sh.async_refresh_health(hass, untrained, now=start)
        )  # grace: nothing yet
        assert fake.issues == {}
        asyncio.run(sh.async_refresh_health(hass, untrained, now=start + UP))
        assert list(fake.issues) == ["e1_forecast_not_trained_a"]
        assert (
            fake.issues["e1_forecast_not_trained_a"]["translation_placeholders"]["name"]
            == "Oven"
        )
        assert fake.issues["e1_forecast_not_trained_a"]["is_fixable"] is False

        trained = _entry(
            FULL_SOLVER, {"a": ("Oven", {"trained_at": "2026-10-06T13:00:00"})}
        )
        asyncio.run(sh.async_refresh_health(hass, trained, now=start + UP + UP))
        assert fake.issues == {}  # cleared on its own


def test_refresh_never_raises():
    hass = SimpleNamespace(data=None)  # setdefault on None raises inside
    asyncio.run(sh.async_refresh_health(hass, _entry(FULL_SOLVER, {})))
