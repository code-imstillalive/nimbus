"""The flattened sensors: one entity per attribute of a parent sensor, so a
household can chart, automate and keep history on each figure (#1837).

Every table is driven the same way: built by its own factory, fed a parent
payload carrying each of its keys, then left without one. Covers what lived
in #335, #849, #1192, #1543 (detailed price forecasts) and the flattened
sensor files.
"""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs
from _module_time import ModuleTime

install_ha_stubs()

fl = importlib.import_module("custom_components.nimbus_load.sensor_flattened")

ENTRY = SimpleNamespace(entry_id="hub1")
DOMAIN = "nimbus_load"

# factory, dispatch, prefix, device identifier suffix
TABLES = {
    "plan": (
        fl.create_flattened_entities,
        fl.dispatch_to_flattened,
        "nimbus_solver",
        "",
    ),
    "p2p": (
        fl.create_flattened_entities_p2p,
        fl.dispatch_to_flattened,
        "nimbus_solver",
        "_p2p",
    ),
    "quality": (
        fl.create_flattened_entities_quality,
        fl.dispatch_to_flattened_quality,
        "nimbus_quality",
        "_quality",
    ),
    "backtest": (
        fl.create_flattened_entities_backtest,
        fl.dispatch_to_flattened_backtest,
        "nimbus_backtest",
        "_backtest",
    ),
    "counterfactual": (
        fl.create_flattened_entities_counterfactual,
        fl.dispatch_to_flattened_counterfactual,
        "nimbus_counterfactual",
        "_counterfactual",
    ),
    "flex": (
        fl.create_flattened_entities_flex,
        fl.dispatch_to_flattened_flex,
        "nimbus_flex",
        "_flex",
    ),
    "flex_report": (
        fl.create_flattened_entities_flex_report,
        fl.dispatch_to_flattened_flex_report,
        "nimbus_flex",
        "_flex",
    ),
}


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(fl, "time", ModuleTime(c))
    return c


def _build(name, hub_device_id=None):
    factory, dispatch, _prefix, _suffix = TABLES[name]
    ents = (
        factory(ENTRY, "0.94.485")
        if name == "plan"
        else factory(ENTRY, "0.94.485", hub_device_id)
    )
    for e in ents:
        e.hass = SimpleNamespace(config=SimpleNamespace(currency="AUD"))
        e.writes = 0

        def write(e=e):
            e.writes += 1

        e.async_write_ha_state = write
    return ents, dispatch


def _payload(specs, value_of):
    """A parent payload that carries every spec's key, dotted keys nested."""
    out: dict = {}
    for spec in specs:
        if "." in spec.source_key:
            top, sub = spec.source_key.split(".", 1)
            out.setdefault(top, {})[sub] = value_of(spec)
        else:
            out[spec.source_key] = value_of(spec)
    return out


def test_each_figure_follows_its_parent_and_keeps_its_last_value_when_absent(
    clock, caplog
):
    # test_each_figure_follows_its_parent_and_keeps_its_last_value_when_absent
    for name in sorted(TABLES):
        ents, dispatch = _build(name)
        payload = _payload(
            [e._spec for e in ents], lambda spec: f"v-{spec.entity_id_suffix}"
        )
        dispatch(ents, payload)
        for e in ents:
            assert e.native_value == f"v-{e._spec.entity_id_suffix}", e.entity_id
            assert e.writes == 1 and e.available
        dispatch(ents, {})  # a payload without these keys changes nothing
        assert all(
            e.native_value == f"v-{e._spec.entity_id_suffix}" and e.writes == 1
            for e in ents
        )

    # test_a_figure_can_carry_a_mapping_as_its_attributes
    # test_a_figure_can_carry_a_mapping_as_its_attributes
    e = _one(_spec(attrs_source_key="detail"))
    e.update_from_parent({"k": 1, "detail": {"a": 1}})
    assert e.extra_state_attributes == {"a": 1, "nimbus_version": "0.94.485"}
    e.update_from_parent({"k": 2, "detail": "ignored"})
    assert e.extra_state_attributes == {"a": 1, "nimbus_version": "0.94.485"}
    assert _one(_spec()).extra_state_attributes == {"nimbus_version": "0.94.485"}

    # test_money_is_shown_in_the_households_currency
    for unit, shown in [
        (fl._CURRENCY, "AUD"),
        (fl._CURRENCY_PER_KWH, "AUD/kWh"),
        ("kWh", "kWh"),
    ]:
        e = _one(_spec(unit_of_measurement=unit))
        assert e.native_unit_of_measurement == shown
        e.hass.config.currency = ""
        assert e.native_unit_of_measurement == (
            None
            if unit == fl._CURRENCY_PER_KWH
            else ("" if unit == fl._CURRENCY else "kWh")
        )
        e.hass = None
        assert e.native_unit_of_measurement == unit

    # test_a_state_at_the_limit_is_kept_and_one_over_is_cut_to_it: HA's limit is inclusive: exactly the limit is a legal state.
    at = "x" * fl._MAX_STATE_LENGTH
    assert fl._clamp_state_length(at, "sensor.a") == at
    over = fl._clamp_state_length(at + "x", "sensor.a")
    assert len(over) == fl._MAX_STATE_LENGTH and over.endswith(
        fl._STATE_TRUNCATION_SUFFIX
    )
    lead = "Grid export at zero (not economical right now)"
    assert fl._clamp_state_length(lead + " " + "y" * 400, "sensor.a").startswith(lead)

    # test_a_figure_goes_stale_after_five_minutes_and_recovers
    caplog.clear()
    e = _one(_spec())
    e.update_from_parent({"k": 1})
    with caplog.at_level(logging.INFO, logger=fl._LOGGER.name):
        clock.now += e._STALE_AFTER_SECONDS
        e._async_recheck_availability(None)
        e._async_recheck_availability(None)
        e.update_from_parent({"k": 2})
        e._async_recheck_availability(None)
    assert e.available and len(e.writes) == 4
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.INFO]

    # test_the_current_figures_read_period_zero_of_the_plan
    # test_the_current_figures_read_period_zero_of_the_plan
    ents = fl.create_flattened_entities_current(ENTRY, "1")
    for e in ents:
        e.hass = None
    plan = _plan()
    fl.dispatch_to_flattened_current(ents, plan)
    battery = [e for e in ents if e._spec.source_key == "battery_kw"]
    assert battery and battery[0].native_value == 0.0
    fl.dispatch_to_flattened_current(ents, {"forecast": []})
    fl.dispatch_to_flattened_current(ents, {"forecast": ["junk"]})
    assert battery[0].native_value == 0.0

    # test_a_price_figure_publishes_the_whole_horizon_as_a_detailed_forecast
    detailed = fl._detailed_price_forecast(_plan(), "import_price")
    rows = detailed["detailedForecast"]
    assert len(rows) == 3
    assert rows[0] == {
        "start": T0.isoformat(),
        "end": (T0 + timedelta(minutes=30)).isoformat(),
        "value": 0.2,
        "value_raw": 0.25,
        "source": "primary",
    }
    meta = detailed["detailed_forecast_meta"]
    assert (meta["direction"], meta["periods"], meta["solve_status"]) == (
        "import",
        3,
        "optimal",
    )
    assert meta["horizon_end"] == (T0 + timedelta(minutes=90)).isoformat()
    for bad in (
        {},
        {"forecast": []},
        {"forecast": [{"time": "x", "hours": 1}, {"no": "time"}, "junk"]},
    ):
        assert fl._detailed_price_forecast(bad, "import_price") is None


def _spec(**kw):
    base = {
        "source_key": "k",
        "name": "K",
        "entity_id_suffix": "k",
        "entity_category": None,
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": None,
        "suggested_display_precision": None,
    }
    return fl.FlattenedAttrSpec(**{**base, **kw})


def _one(spec, hass=True):
    e = fl._FlattenedAttributeSensor(ENTRY, "0.94.485", spec)
    e.hass = SimpleNamespace(config=SimpleNamespace(currency="AUD")) if hass else None
    e.writes = []
    e.async_write_ha_state = lambda: e.writes.append(e.native_value)
    return e


# --- the current period and its detailed price forecast ----------------------------------

T0 = datetime(2026, 9, 20, 16, 0).astimezone()


def _plan(n=3):
    return {
        "status": "optimal",
        "forecast": [
            {
                "time": (T0 + timedelta(minutes=30 * i)).isoformat(),
                "hours": 0.5,
                "import_price": 0.2 + i,
                "import_price_raw": 0.25,
                "import_price_source": "primary",
                "battery_kw": float(i),
            }
            for i in range(n)
        ],
    }


# --- the hub device -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "found"),
    [("found", "dev-1"), ("missing", None), ("broken", None), ("old_ha", None)],
)
def test_the_hub_device_is_found_when_home_assistant_can_say(
    monkeypatch, behaviour, found
):
    def lookup(hass, identifier, config_entry_id):
        if behaviour == "missing":
            raise ValueError("no device")
        if behaviour == "broken":
            raise RuntimeError("registry")
        return "dev-1"

    if behaviour == "old_ha":
        monkeypatch.delattr(fl.dr, "async_get_device_id_by_identifier", raising=False)
    else:
        monkeypatch.setattr(
            fl.dr, "async_get_device_id_by_identifier", lookup, raising=False
        )
    assert fl.resolve_hub_device_id(None, ENTRY) == found
    assert fl.resolve_via_device_field(found, "hub1") == (
        {"via_device_id": found} if found else {"via_device": (DOMAIN, "hub1")}
    )


def test_every_table_key_is_distinct_within_its_table():
    # test_every_table_key_is_distinct_within_its_table
    for table in (
        fl.FLATTENED_ATTRS,
        fl.FLATTENED_ATTRS_QUALITY,
        fl.FLATTENED_ATTRS_FLEX,
        fl.FLATTENED_ATTRS_CURRENT,
    ):
        suffixes = [s.entity_id_suffix for s in table]
        assert len(suffixes) == len(set(suffixes))

    # test_the_state_length_limit_is_home_assistants_own: Defined locally so the stubs need not carry it; the real constant
    real = pytest.importorskip("homeassistant.const")
    if not hasattr(real, "MAX_LENGTH_STATE_STATE"):
        pytest.skip("the installed homeassistant.const is a stub")
    assert fl._MAX_STATE_LENGTH == real.MAX_LENGTH_STATE_STATE

    # test_what_gets_its_own_history_and_what_deliberately_does_not
    for key, has_a_child in [
        # #849: varying numbers that had no history at all.
        ("envelope_import_limit_kw", True),
        ("envelope_export_limit_kw", True),
        ("solar_risk_effect_now_kw", True),
        ("import_price_risk_effect_now", True),
        ("export_price_risk_effect_now", True),
        ("load_whole_house_live_now_kw", True),
        # Left out on measured grounds: constants, dicts, a 762-char text.
        ("battery_kw_side", False),
        ("battery_kw_sign_convention", False),
        ("efficiency_convention", False),
        ("load_forecast_source_used", False),
        ("solve_diagnostics", False),
    ]:
        assert (key in _flattened_keys()) is has_a_child


def _flattened_keys():
    return {
        spec.source_key
        for name in dir(fl)
        if name.startswith("FLATTENED_ATTRS")
        for spec in getattr(fl, name)
    }
