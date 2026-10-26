"""The hub's options wizard: each page saves exactly its own settings, the
Solver pages carry their answers through to one save, the live battery and
grid numbers are written on that save, and a page that cannot be saved says
why (#1837).

Covers what lived in #1067 (advanced sections, suggestions), #1213, #1566,
#1589, #1686 (confirmed pricing suggestions), #1800 (live numbers) and the
hub-options files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

ho = importlib.import_module("custom_components.nimbus_load.flows.hub_options")
ln = importlib.import_module("custom_components.nimbus_load.flows.live_numbers")
const = importlib.import_module("custom_components.nimbus_load.const")
DOMAIN = const.DOMAIN


def _run(coro):
    return asyncio.run(coro)


class Number:
    def __init__(self):
        self.set_to = None

    async def async_set_native_value(self, value):
        self.set_to = value


class Hass:
    def __init__(self, states=None):
        self._states = dict(states or {})
        self.states = SimpleNamespace(
            get=self._states.get, async_all=lambda domain=None: []
        )
        self.calls = []
        self.numbers = {}
        self.data = {
            "entity_components": {
                "number": SimpleNamespace(get_entity=self.numbers.get)
            }
        }

        async def call(domain, service, data, *a, **kw):
            self.calls.append((domain, service, data))

        self.services = SimpleNamespace(async_call=call)


@pytest.fixture
def world(monkeypatch):
    """Suggestions off unless a test sets them; one registry for the hub's numbers."""
    registry = {}

    def get_entity_id(domain, platform, unique_id):
        return registry.get(unique_id)

    def get(entity_id):
        return (
            SimpleNamespace(platform=DOMAIN) if entity_id in registry.values() else None
        )

    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(async_get_entity_id=get_entity_id, async_get=get),
        raising=False,
    )
    suggestions = {"pricing": [], "dashboard": {}, "switchboard": {}, "typed": {}}

    async def dashboard(hass):
        return dict(suggestions["dashboard"])

    async def switchboard(hass):
        return dict(suggestions["switchboard"])

    monkeypatch.setattr(ho, "_energy_dashboard_solver_source_suggestions", dashboard)
    monkeypatch.setattr(ho, "_energy_dashboard_switchboard_suggestions", switchboard)
    monkeypatch.setattr(
        ho, "_type_safe_entity_suggestions", lambda hass: dict(suggestions["typed"])
    )
    monkeypatch.setattr(
        ho, "_discover_nimbus_load_forecast_candidates", lambda hass: ([], [])
    )
    monkeypatch.setattr(
        ho,
        "pricing_suggestions",
        lambda hass, options, dashboard: list(suggestions["pricing"]),
    )
    monkeypatch.setattr(ho, "frozen_price_field_errors", lambda hass, data, fields: {})
    monkeypatch.setattr(
        ho, "energy_unit_field_errors", lambda hass, user_input, fields: {}
    )
    return SimpleNamespace(registry=registry, suggestions=suggestions)


def _flow(options=None, hass=None):
    entry = SimpleNamespace(entry_id="hub1", options=dict(options or {}))
    flow = ho.NimbusHubOptionsFlow(entry)
    flow.hass = hass or Hass()
    return flow


def test_the_menu_offers_pricing_suggestions_only_when_there_are_some(world):
    assert _run(_flow().async_step_init())["menu_options"] == [
        "forecaster",
        "solver_battery",
        "switchboard",
    ]
    world.suggestions["pricing"] = [
        {
            "label": "Import price",
            "field": "f",
            "proposed": "sensor.p",
            "source": "Amber",
        }
    ]
    assert _run(_flow().async_step_init())["menu_options"][-1] == "pricing_suggestions"


# --- the Solver pages, carried through to one save ---------------------------------


BATTERY = {"solver_battery_soc_sensor": "sensor.soc"}
GRID = {
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
}
SOURCES = {
    "solver_solar_forecast_sensor": "sensor.solar_fc",
    "solver_load_forecast_sensor": "sensor.load_fc",
}


def test_the_solver_pages_save_once_at_the_end_with_every_answer(world):
    hass = Hass()
    flow = _flow(
        {"other": 1, "solver_import_price_sensor_2": "sensor.old_secondary"}, hass
    )
    form = _run(flow.async_step_solver_battery())
    assert form["step_id"] == "solver_battery"
    grid_form = _run(flow.async_step_solver_battery(dict(BATTERY)))
    assert grid_form["step_id"] == "solver_grid"
    sources_form = _run(
        flow.async_step_solver_grid(
            {**GRID, "advanced": {"solver_price_event_sensor": "sensor.event"}}
        )
    )
    assert sources_form["step_id"] == "solver_sources"
    saved = _run(flow.async_step_solver_sources(dict(SOURCES)))
    assert saved["type"] == "create_entry"
    data = saved["data"]
    assert data["other"] == 1  # settings from elsewhere are kept
    assert {k: data[k] for k in (*BATTERY, *GRID, *SOURCES)} == {
        **BATTERY,
        **GRID,
        **SOURCES,
    }
    assert (
        data["solver_price_event_sensor"] == "sensor.event"
    )  # read out of the advanced section
    # A field the page showed and the household left empty is cleared, not kept.
    assert data["solver_import_price_sensor_2"] is None
    assert (
        "persistent_notification",
        "dismiss",
        {"notification_id": "nimbus_setup_incomplete"},
    ) in hass.calls


def test_battery_and_grid_sizes_typed_in_the_wizard_are_written_to_their_live_numbers(
    world,
):
    hass = Hass()
    for key in (
        const.CONF_SOLVER_BATTERY_CAPACITY_KWH,
        const.CONF_SOLVER_GRID_MAX_IMPORT_KW,
        const.CONF_SOLVER_MAX_CHARGE_KW,
    ):
        eid = f"number.nimbus_{key}"
        world.registry[f"hub1_{key}"] = eid
        hass.numbers[eid] = Number()
    flow = _flow({}, hass)
    _run(
        flow.async_step_solver_battery(
            {
                **BATTERY,
                ln.PREFIX + const.CONF_SOLVER_BATTERY_CAPACITY_KWH: 13.5,
                ln.PREFIX + const.CONF_SOLVER_MAX_CHARGE_KW: "",
            }
        )
    )
    _run(flow.async_step_solver_grid({**GRID, ln.GRID_IMPORT_FIELD: 20}))
    saved = _run(flow.async_step_solver_sources(dict(SOURCES)))
    assert (
        hass.numbers[f"number.nimbus_{const.CONF_SOLVER_BATTERY_CAPACITY_KWH}"].set_to
        == 13.5
    )
    assert (
        hass.numbers[f"number.nimbus_{const.CONF_SOLVER_GRID_MAX_IMPORT_KW}"].set_to
        == 20.0
    )
    assert (
        hass.numbers[f"number.nimbus_{const.CONF_SOLVER_MAX_CHARGE_KW}"].set_to is None
    )  # left empty
    assert not any(k.startswith(ln.PREFIX) for k in saved["data"])


def test_a_frozen_price_keeps_the_grid_page_open_with_the_reason(world, monkeypatch):
    monkeypatch.setattr(
        ho,
        "frozen_price_field_errors",
        lambda hass, data, fields: {
            "solver_import_price_sensor": "frozen_price",
            "solver_price_forecast_array_sensor": "frozen_price",
        },
    )
    flow = _flow()
    _run(flow.async_step_solver_battery(dict(BATTERY)))
    again = _run(flow.async_step_solver_grid(dict(GRID)))
    assert again["step_id"] == "solver_grid"
    # A field on the page gets its own error; one that is not, the page's.
    assert again["errors"] == {
        "solver_import_price_sensor": "frozen_price",
        "base": "frozen_price",
    }


def test_the_forms_start_from_saved_values_then_suggestions(world):
    world.suggestions["dashboard"] = {
        "solver_battery_soc_sensor": "sensor.suggested_soc"
    }
    form = _run(_flow().async_step_solver_battery())
    schema = form["data_schema"]
    soc = next(k for k in schema.schema if str(k) == "solver_battery_soc_sensor")
    assert soc.default() == "sensor.suggested_soc"
    saved = _run(
        _flow({"solver_battery_soc_sensor": "sensor.mine"}).async_step_solver_battery()
    )
    soc = next(
        k for k in saved["data_schema"].schema if str(k) == "solver_battery_soc_sensor"
    )
    assert soc.default() == "sensor.mine"


# --- the forecaster and switchboard pages --------------------------------------------


def test_the_forecaster_page_saves_its_own_fields_only(world):
    flow = _flow({"other": 1, "humidity_sensor": "sensor.old"})
    form = _run(flow.async_step_forecaster())
    assert form["step_id"] == "forecaster"
    saved = _run(
        flow.async_step_forecaster(
            {"temperature_sensor": "sensor.t", "forecast_horizon_hours": 120}
        )
    )
    assert (
        saved["data"]["temperature_sensor"] == "sensor.t"
        and saved["data"]["other"] == 1
    )
    assert saved["data"]["humidity_sensor"] is None
    assert set(saved["data"]) == {"other", *ho._FORECASTER_SCHEMA_KEYS}


def test_an_energy_sensor_offered_as_power_keeps_the_forecaster_page_open(
    world, monkeypatch
):
    monkeypatch.setattr(
        ho,
        "energy_unit_field_errors",
        lambda hass, ui, fields: {"battery_sensor": "energy_unit"},
    )
    again = _run(
        _flow().async_step_forecaster({"battery_sensor": "sensor.battery_kwh"})
    )
    assert again["type"] == "form" and again["errors"] == {
        "battery_sensor": "energy_unit"
    }


def test_the_switchboard_page_saves_its_own_fields_only(world):
    world.suggestions["switchboard"] = {
        "switchboard_import_price_sensor": "sensor.dash_buy"
    }
    flow = _flow({"other": 2})
    form = _run(flow.async_step_switchboard())
    field = next(
        k
        for k in form["data_schema"].schema
        if str(k) == "switchboard_import_price_sensor"
    )
    assert field.description == {
        "suggested_value": "sensor.dash_buy"
    }  # suggested, not saved
    saved = _run(
        flow.async_step_switchboard({"switchboard_import_price_sensor": "sensor.buy"})
    )
    assert set(saved["data"]) == {"other", *ho._SWITCHBOARD_SCHEMA_KEYS}
    assert saved["data"]["switchboard_import_price_sensor"] == "sensor.buy"


@pytest.mark.parametrize(
    ("value", "hours"),
    [
        (48, const.MIN_FORECAST_HORIZON_HOURS),
        (200, 200),
        ("x", const.DEFAULT_FORECAST_HORIZON_HOURS),
    ],
)
def test_the_forecast_horizon_is_never_shorter_than_the_solvers(value, hours):
    assert ho._at_least_solver_horizon(value) == hours


# --- confirmed pricing suggestions ------------------------------------------------------


def test_pricing_suggestions_are_applied_only_when_confirmed_and_announced(
    world, monkeypatch
):
    suggestion = {
        "label": "Import price",
        "field": "solver_import_price_sensor",
        "proposed": "sensor.amber",
        "source": "Amber",
    }
    world.suggestions["pricing"] = [suggestion]
    monkeypatch.setattr(
        ho,
        "apply_pricing_suggestions",
        lambda options, suggestions: (
            {**options, "solver_import_price_sensor": "sensor.amber"},
            list(suggestions),
        ),
    )
    hass = Hass()
    flow = _flow({"other": 1}, hass)
    shown = []
    flow.async_show_form = lambda **kw: shown.append(kw) or {"type": "form", **kw}
    form = _run(flow.async_step_pricing_suggestions())
    assert (
        "(empty) -> `sensor.amber` (from Amber)"
        in shown[0]["description_placeholders"]["suggestions"]
    )
    assert hass.calls == []
    saved = _run(flow.async_step_pricing_suggestions({}))
    assert saved["data"] == {"other": 1, "solver_import_price_sensor": "sensor.amber"}
    ((_domain, service, data),) = hass.calls
    assert (
        service == "create"
        and "Import price: (empty) -> `sensor.amber`" in data["message"]
    )
    assert form["type"] == "form"


def test_with_no_suggestions_left_the_page_closes(world):
    flow = _flow()
    flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
    assert _run(flow.async_step_pricing_suggestions()) == {
        "type": "abort",
        "reason": "no_pricing_suggestions",
    }


# --- the advanced section ------------------------------------------------------------------


def test_optional_fields_fold_into_one_advanced_section_and_unfold_on_save():
    schema = vol.Schema(
        {vol.Required("a"): str, vol.Optional("b"): str, vol.Optional("c"): str}
    )
    folded = ho._collapse_optionals_into_advanced(schema, keep_visible=("c",))
    assert [str(k) for k in folded.schema] == ["a", "c", ho._ADVANCED_SECTION]
    assert ho._effective_schema_keys(folded) == ["a", "c", "b"]
    assert ho._flatten_section_input(
        folded, {"a": 1, ho._ADVANCED_SECTION: {"b": 2}}
    ) == {"a": 1, "b": 2}
    assert ho._collapse_optionals_into_advanced(
        vol.Schema({vol.Required("a"): str})
    ).schema.keys() == {"a"}


@pytest.mark.parametrize(
    ("candidates", "saved", "out"),
    [
        (None, "sensor.x", None),
        (["sensor.a"], None, ["sensor.a"]),
        (["sensor.a"], "sensor.a", ["sensor.a"]),
        (["sensor.a"], "sensor.gone", ["sensor.a", "sensor.gone"]),
        (["sensor.a"], ["sensor.b", "", "sensor.a"], ["sensor.a", "sensor.b"]),
    ],
)
def test_a_saved_value_stays_selectable_even_when_no_longer_suggested(
    candidates, saved, out
):
    assert ho._with_saved_values(candidates, saved) == out


# --- live numbers ---------------------------------------------------------------------------


def test_live_number_fields_show_the_current_value_but_never_the_placeholder(world):
    hass = Hass(
        {
            "number.cap": SimpleNamespace(state="13.5"),
            "number.charge": SimpleNamespace(state="0.1"),
        }
    )
    world.registry["hub1_" + const.CONF_SOLVER_BATTERY_CAPACITY_KWH] = "number.cap"
    world.registry["hub1_" + const.CONF_SOLVER_MAX_CHARGE_KW] = "number.charge"
    assert (
        ln.current_value(hass, "hub1", const.CONF_SOLVER_BATTERY_CAPACITY_KWH) == 13.5
    )
    assert ln.current_value(hass, "hub1", const.CONF_SOLVER_MAX_CHARGE_KW) is None
    assert ln.current_value(hass, "hub1", const.CONF_SOLVER_MAX_DISCHARGE_KW) is None
    fields = ln.schema_fields(hass, "hub1", "solver_battery")
    assert [str(k) for k in fields] == [
        ln.PREFIX + key for key, *_ in ln.STEP_NUMBERS["solver_battery"]
    ]
    assert (
        ln.schema_fields(hass, None, "solver_battery") == {}
        and ln.schema_fields(hass, "hub1", "nope") == {}
    )


def _default_of(form, key):
    for marker in form["data_schema"].schema:
        if str(marker) == key:
            d = getattr(marker, "default", None)
            value = d() if callable(d) else d
            sv = (getattr(marker, "description", None) or {}).get("suggested_value")
            return sv if sv is not None else value
    return None


def test_the_energy_dashboard_fills_only_what_the_household_has_not_saved(world):
    """#1067: the dashboard's battery sensor is offered when the field is
    empty, and a saved choice always wins over it."""
    world.suggestions["dashboard"] = {
        "solver_battery_soc_sensor": "sensor.dashboard_soc"
    }
    empty = _run(_flow().async_step_solver_battery())
    assert _default_of(empty, "solver_battery_soc_sensor") == "sensor.dashboard_soc"
    saved = _run(
        _flow(
            {"solver_battery_soc_sensor": "sensor.saved_soc"}
        ).async_step_solver_battery()
    )
    assert _default_of(saved, "solver_battery_soc_sensor") == "sensor.saved_soc"
