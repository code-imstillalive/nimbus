"""Setup health: the Repairs a household sees for a setup that cannot work
yet, each with the words the translation needs, raised and cleared as the
setup changes (#1837).

Covers what lived in #1360, #1466, #1562, #1587, #1643, #1825 and the
setup-health files.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sh = importlib.import_module("custom_components.nimbus_load.setup_health")
const = importlib.import_module("custom_components.nimbus_load.const")

OPTIONS = {key: f"sensor.{key}" for key, _label in sh._REQUIRED_SOLVER_INPUTS}
UP = sh.TRAIN_GRACE
T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
ISSUES = json.loads(
    (Path(sh.__file__).parent / "strings.json").read_text(encoding="utf-8")
)["issues"]


def _kinds(issues):
    return [i.kind for i in issues]


def _health(**kw):
    kw.setdefault("options", OPTIONS)
    kw.setdefault("forecasts", ())
    kw.setdefault("hub_up_for", UP)
    return sh.evaluate_health(**kw)


def test_a_complete_setup_has_no_repairs():
    assert _health() == []


def test_every_repair_has_a_translation_with_exactly_its_placeholders():
    every = _health(
        options={},
        forecasts=[("s1", "Pool", {})],
        energy_unit_inputs=[("Battery power", "sensor.e", "kWh")],
        non_power_unit_inputs=[("Grid power", "sensor.a", "A")],
        frozen_price_inputs=[("Import price", "sensor.p", "unchanged for 3 days")],
        fees_doubled=("sensor.flex_up", "network fees"),
        fees_on_tariff=("sensor.pd7", "fees"),
        regional_forecast_missing=("AEMO", "QLD1"),
        settings={
            const.CONF_SOLVER_BATTERY_CAPACITY_KWH: 0.0,
            const.CONF_SOLVER_BATTERY_MIN_SOC_PERCENT: 90,
            const.CONF_SOLVER_BATTERY_MAX_SOC_PERCENT: 80,
        },
        has_load_forecast=True,
        failed_streak={"count": 5, "since": "2026-09-20T06:00:00+00:00"},
        availability_mismatches=[("p1", "Car", "sensor.c", ("Charge",), "Charging")],
    )
    infeasible = sh._plan_failing_issue({"count": 5, "status": "infeasible"})
    every.append(infeasible)
    assert len({i.kind for i in every}) == 14
    for issue in every:
        words = set(re.findall(r"\{(\w+)\}", json.dumps(ISSUES[issue.kind])))
        assert words == set(issue.placeholders), issue.kind


def test_an_untrained_forecast_is_a_repair_only_after_the_grace_period():
    pending = [("s1", "Pool pump", {"last_retrain_error": "  only 57 usable points  "})]
    assert _health(forecasts=pending, hub_up_for=UP - timedelta(seconds=1)) == []
    (issue,) = _health(forecasts=pending)
    assert issue.key == f"{sh.KIND_NOT_TRAINED}_s1"
    assert issue.placeholders == {
        "name": "Pool pump",
        "reason": "only 57 usable points",
    }


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (None, "it has not found enough usable history yet"),
        ({"forecast_readiness": "incomplete_rule"}, "set the start and end hours"),
    ],
)
def test_an_untrained_forecast_says_why(data, reason):
    (issue,) = _health(forecasts=[("s1", "Pool", data)])
    assert reason in issue.placeholders["reason"]


@pytest.mark.parametrize(
    "data", [{"trained_at": "t"}, {"forecast": [1]}, {"forecast_readiness": "ready"}]
)
def test_a_trained_or_rule_based_forecast_needs_nothing(data):
    assert _health(forecasts=[("s1", "Pool", data)]) == []


def test_bad_inputs_are_listed_one_per_line():
    issues = _health(
        energy_unit_inputs=[
            ("Battery power", "sensor.e", "kWh"),
            ("Solar power", "sensor.s", "Wh"),
        ],
        frozen_price_inputs=[("Import price", "sensor.p", "unchanged")],
    )
    by_kind = {i.kind: i for i in issues}
    assert (
        by_kind[sh.KIND_ENERGY_UNIT].placeholders["inputs"]
        == "- Battery power: `sensor.e` reports kWh\n- Solar power: `sensor.s` reports Wh"
    )
    assert (
        by_kind[sh.KIND_FROZEN_PRICE].placeholders["inputs"]
        == "- Import price: `sensor.p` (unchanged)"
    )


@pytest.mark.parametrize(
    ("settings", "says"),
    [
        (
            {const.CONF_SOLVER_MAX_CHARGE_KW: 0.1},
            "Max charge power (Battery card) is 0.1",
        ),
        ({const.CONF_SOLVER_MAX_CHARGE_KW: 0.11}, None),
        (
            {
                const.CONF_SOLVER_BATTERY_MIN_SOC_PERCENT: 50,
                const.CONF_SOLVER_BATTERY_MAX_SOC_PERCENT: 50,
            },
            "Min SoC (50%) is not below Max SoC (50%)",
        ),
        (
            {
                const.CONF_SOLVER_CHARGE_BY_PERCENT: 95,
                const.CONF_SOLVER_BATTERY_MAX_SOC_PERCENT: 90,
            },
            "Charge By's target (95%) is above Max SoC (90%)",
        ),
        (
            {
                const.CONF_SOLVER_CHARGE_BY_PERCENT: 95,
                const.CONF_SOLVER_BATTERY_MAX_SOC_PERCENT: 0,
            },
            None,
        ),
    ],
)
def test_placeholder_and_contradictory_settings_are_named(settings, says):
    issues = _health(settings=settings)
    if says is None:
        assert issues == []
    else:
        (issue,) = issues
        assert says in issue.placeholders["settings"]


def test_missing_solver_inputs_and_the_whole_house_check_are_named():
    issues = _health(
        options={k: v for k, v in OPTIONS.items() if "price" not in k},
        has_load_forecast=True,
    )
    by_kind = {i.kind: i for i in issues}
    assert (
        by_kind[sh.KIND_SOLVER_INPUT].placeholders["inputs"]
        == "import price, export price"
    )
    assert sh.KIND_WHOLE_HOUSE_UNSET in by_kind
    covered = _health(
        options={
            **OPTIONS,
            const.CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: "sensor.house",
        },
        has_load_forecast=True,
    )
    assert covered == []


@pytest.mark.parametrize(
    ("streak", "kind", "since"),
    [
        ({"count": 4}, None, None),
        (
            {"count": 5, "since": "2026-09-20T20:45:00+10:00", "raw_status": "Error"},
            sh.KIND_PLAN_FAILING_ERROR,
            "20:45",
        ),
        (
            {"count": 9, "since": "x", "status": "infeasible"},
            sh.KIND_PLAN_FAILING_INFEASIBLE,
            "x",
        ),
        ({"count": 5}, sh.KIND_PLAN_FAILING_ERROR, "unknown"),
    ],
)
def test_a_plan_failing_five_cycles_running_is_a_repair(streak, kind, since):
    issue = sh._plan_failing_issue(streak)
    if kind is None:
        assert issue is None
        return
    assert issue.kind == kind and issue.key == sh.KIND_PLAN_FAILING
    assert issue.placeholders["since"] == since and issue.placeholders["cycles"] == str(
        streak["count"]
    )


# --- raised and cleared in Home Assistant ------------------------------------------------


@pytest.fixture
def repairs(monkeypatch):
    raised, deleted = {}, []
    ir = types.ModuleType("homeassistant.helpers.issue_registry")
    ir.IssueSeverity = SimpleNamespace(WARNING="warning")
    ir.async_create_issue = lambda hass, domain, issue_id, **kw: raised.__setitem__(
        issue_id, kw
    )
    ir.async_delete_issue = lambda hass, domain, issue_id: deleted.append(issue_id)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", ir)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "issue_registry", ir, raising=False
    )
    for module, names in {
        "power_input_check": ("find_energy_unit_inputs", "find_non_power_unit_inputs"),
        "price_input_check": ("find_frozen_price_inputs",),
        "pricing_autodetect": (
            "detect_fees_doubled",
            "detect_fees_on_pd7day_tariff",
            "detect_missing_regional_forecast",
        ),
    }.items():
        mod = importlib.import_module(f"custom_components.nimbus_load.{module}")
        for name in names:
            result = [] if name.startswith("find") else None
            monkeypatch.setattr(mod, name, lambda *a, result=result: result)
    monkeypatch.setattr(sh, "_live_settings", lambda hass, entry: {})
    monkeypatch.setattr(sh, "_solver_failed_streak", lambda: None)
    entry = SimpleNamespace(
        entry_id="hub1", options=dict(OPTIONS), subentries={}, runtime_data={}
    )
    hass = SimpleNamespace(data={})
    return hass, entry, raised, deleted


def test_a_repair_is_raised_while_it_stands_and_deleted_once_fixed(repairs):
    hass, entry, raised, deleted = repairs
    entry.options.pop(const.CONF_SOLVER_IMPORT_PRICE_SENSOR)
    issues = asyncio.run(sh.async_refresh_health(hass, entry, now=T0))
    assert [i.kind for i in issues] == [sh.KIND_SOLVER_INPUT]
    assert (
        raised["hub1_solver_input_missing"]["translation_key"] == sh.KIND_SOLVER_INPUT
    )
    assert raised["hub1_solver_input_missing"]["translation_placeholders"] == {
        "inputs": "import price"
    }
    entry.options[const.CONF_SOLVER_IMPORT_PRICE_SENSOR] = "sensor.price"
    assert asyncio.run(sh.async_refresh_health(hass, entry, now=T0)) == []
    assert deleted == ["hub1_solver_input_missing"]


def test_the_grace_period_runs_from_the_first_refresh(repairs):
    hass, entry, raised, _ = repairs
    entry.runtime_data = {"s1": SimpleNamespace(data={})}
    entry.subentries = {
        "s1": SimpleNamespace(title="Pool", subentry_type=const.SUBENTRY_TYPE_LOAD)
    }
    entry.options[const.CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR] = "sensor.house"
    assert asyncio.run(sh.async_refresh_health(hass, entry, now=T0)) == []
    later = asyncio.run(sh.async_refresh_health(hass, entry, now=T0 + sh.TRAIN_GRACE))
    assert [i.key for i in later] == [f"{sh.KIND_NOT_TRAINED}_s1"]
    assert (
        raised[f"hub1_{sh.KIND_NOT_TRAINED}_s1"]["translation_placeholders"]["name"]
        == "Pool"
    )


def test_a_failing_check_never_breaks_the_refresh(repairs, monkeypatch):
    hass, entry, _, _ = repairs

    def boom(*a):
        raise RuntimeError("registry down")

    monkeypatch.setattr(sh, "evaluate_health", boom)
    assert asyncio.run(sh.async_refresh_health(hass, entry, now=T0)) == []


def test_solve_outcomes_refresh_health_only_when_failing_starts_or_stops(monkeypatch):
    refreshed = []

    async def refresh(hass, entry):
        refreshed.append(entry.entry_id)

    monkeypatch.setattr(sh, "async_refresh_health", refresh)
    streak = {"value": None}
    monkeypatch.setattr(sh, "_solver_failed_streak", lambda: streak["value"])
    hass, entry = SimpleNamespace(data={}), SimpleNamespace(entry_id="hub1")
    asyncio.run(sh.async_note_solve_outcome(hass, entry))
    streak["value"] = {"count": 5}
    asyncio.run(sh.async_note_solve_outcome(hass, entry))
    asyncio.run(sh.async_note_solve_outcome(hass, entry))
    streak["value"] = {"count": 0}
    asyncio.run(sh.async_note_solve_outcome(hass, entry))
    assert refreshed == ["hub1", "hub1"]


def test_live_settings_are_read_from_the_number_entities(monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    ids = {
        f"hub1_{const.CONF_SOLVER_BATTERY_CAPACITY_KWH}": "number.cap",
        f"hub1_{const.CONF_SOLVER_MAX_CHARGE_KW}": "number.charge",
    }
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get_entity_id=lambda d, p, uid: ids.get(uid)
        ),
        raising=False,
    )
    states = {
        "number.cap": SimpleNamespace(state="13.5"),
        "number.charge": SimpleNamespace(state="unknown"),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    assert sh._live_settings(hass, SimpleNamespace(entry_id="hub1")) == {
        const.CONF_SOLVER_BATTERY_CAPACITY_KWH: 13.5
    }
    monkeypatch.setattr(er, "async_get", lambda hass: 1 / 0, raising=False)
    assert sh._live_settings(hass, SimpleNamespace(entry_id="hub1")) == {}


def test_the_failed_streak_comes_from_the_solver_publisher_when_it_is_loaded(
    monkeypatch,
):
    pub = types.ModuleType("solver_publish")
    pub.failed_streak = lambda: {"count": 3}
    monkeypatch.setitem(sys.modules, "solver_publish", pub)
    monkeypatch.delitem(sys.modules, f"{sh.__package__}.solver_publish", raising=False)
    assert sh._solver_failed_streak() == {"count": 3}
    pub.failed_streak = lambda: 1 / 0
    assert sh._solver_failed_streak() is None
    monkeypatch.delitem(sys.modules, "solver_publish")
    assert sh._solver_failed_streak() is None
