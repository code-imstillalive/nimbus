"""The integration's services: registered once, removed on unload, and each
one's answer to a good call and to every way a call can be wrong (#1837).

Covers what lived in #495, #809/#811, #1200, #1574 (device contract
services), #1657 (decision inputs) and the services files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

svc = importlib.import_module("custom_components.nimbus_load.services")
const = importlib.import_module("custom_components.nimbus_load.const")
DOMAIN = const.DOMAIN
BNE = ZoneInfo("Australia/Brisbane")
NOW = datetime(2026, 9, 20, 16, 0, tzinfo=BNE)
Invalid = svc.ServiceValidationError
Failed = sys.modules["homeassistant.exceptions"].HomeAssistantError


class Services:
    def __init__(self):
        self.handlers = {}
        self.schemas = {}

    def has_service(self, domain, service):
        return (domain, service) in self.handlers

    def async_register(
        self, domain, service, handler, schema=None, supports_response=None
    ):
        self.handlers[(domain, service)] = handler
        self.schemas[service] = schema

    def async_remove(self, domain, service):
        del self.handlers[(domain, service)]


class Entries:
    def __init__(self, entries):
        self.entries = entries
        self.updated, self.added, self.reloaded = [], [], []

    def async_entries(self, domain):
        return list(self.entries)

    def async_update_subentry(self, entry, subentry, title, data):
        self.updated.append((subentry.subentry_id, title, data))

    def async_add_subentry(self, entry, subentry):
        subentry.subentry_id = "new1"
        self.added.append(subentry)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)


def _hass(entries=(), states=None):
    states = states or {}
    hass = SimpleNamespace(
        services=Services(),
        config_entries=Entries(list(entries)),
        states=SimpleNamespace(get=states.get),
    )

    async def executor(fn, *a):
        return fn(*a)

    hass.async_add_executor_job = executor
    svc.async_register_services(hass)
    return hass


def _call(hass, service, data=None):
    schema = hass.services.schemas[service]
    data = schema(dict(data or {})) if schema is not None else dict(data or {})
    return asyncio.run(
        hass.services.handlers[(DOMAIN, service)](SimpleNamespace(data=data))
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(svc.dt_util, "now", lambda: NOW)
    monkeypatch.setattr(svc.dt_util, "as_utc", lambda t: t.astimezone(UTC))
    monkeypatch.setattr(
        svc.dt_util,
        "parse_datetime",
        lambda s: datetime.fromisoformat(s) if "T" in s else None,
    )
    monkeypatch.setattr(svc.dt_util, "DEFAULT_TIME_ZONE", BNE)


def _hub(**subentries):
    return SimpleNamespace(entry_id="hub1", subentries=subentries, runtime_data={})


# --- registration -----------------------------------------------------------------


def test_every_documented_service_is_registered():
    import yaml

    documented = set(
        yaml.safe_load(
            (Path(svc.__file__).parent / "services.yaml").read_text(encoding="utf-8")
        )
    )
    registered = {s for _d, s in _hass().services.handlers}
    assert documented <= registered
    # Not yet described for Developer Tools: see the #1837 findings.
    assert registered - documented == {svc.SERVICE_SET_CONTROLLABLE_LOAD}


# --- argument parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20T10:00:00+10:00", datetime(2026, 9, 20, 0, 0, tzinfo=UTC)),
        (
            "2026-09-20T10:00:00",
            datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
        ),  # naive: local
        (datetime(2026, 9, 20, 10, 0), datetime(2026, 9, 20, 0, 0, tzinfo=UTC)),  # noqa: DTZ001
    ],
)
def test_a_window_bound_is_any_iso_time_read_as_local_when_naive(value, expected):
    assert svc._coerce_datetime(value) == expected


@pytest.mark.parametrize("value", ["yesterday", 5])
def test_a_window_bound_that_is_not_a_time_is_refused(value):
    with pytest.raises(vol.Invalid):
        svc._coerce_datetime(value)


@pytest.mark.parametrize(
    ("value", "kinds"),
    [("head", ["head"]), (["horizon", "head", "head"], ["horizon", "head"])],
)
def test_decision_input_kinds_are_head_horizon_or_both(value, kinds):
    assert svc._coerce_kinds(value) == kinds


# --- retrain and solve now -----------------------------------------------------------


def _coordinator():
    c = SimpleNamespace(retrained=0)

    async def retrain():
        c.retrained += 1
        if c.retrained > 5:
            raise RuntimeError("never")

    c._async_retrain = retrain
    return c


def test_retrain_runs_every_forecaster_or_only_the_ones_named(monkeypatch):
    a, b = _coordinator(), _coordinator()
    hub = _hub()
    hub.runtime_data = {"subA": a, "subB": b}
    hass = _hass([hub])
    registry = {
        "sensor.a_forecast": SimpleNamespace(unique_id="subA_load_forecast"),
        "sensor.b_forecast": SimpleNamespace(unique_id="subB_signal_forecast"),
        "sensor.other": SimpleNamespace(unique_id="something_else"),
    }
    monkeypatch.setattr(
        svc.er, "async_get", lambda hass: SimpleNamespace(async_get=registry.get)
    )
    monkeypatch.setattr(
        svc.cv, "entity_ids", lambda v: [v] if isinstance(v, str) else list(v)
    )
    _call(hass, svc.SERVICE_RETRAIN)
    assert (a.retrained, b.retrained) == (1, 1)
    _call(hass, svc.SERVICE_RETRAIN, {"entity_id": ["sensor.b_forecast"]})
    assert (a.retrained, b.retrained) == (1, 2)
    with pytest.raises(Invalid, match="sensor.other, sensor.missing"):
        _call(
            hass,
            svc.SERVICE_RETRAIN,
            {"entity_id": ["sensor.a_forecast", "sensor.other", "sensor.missing"]},
        )
    assert a.retrained == 1  # nothing ran when any name was wrong
    _call(_hass(), svc.SERVICE_RETRAIN)  # no forecasters yet: nothing to do


@pytest.mark.parametrize("ok", [True, False])
def test_solve_now_runs_a_solve_and_records_only_a_successful_one(
    monkeypatch, ok, caplog
):
    recorded = []

    async def solve(hass):
        return ok

    monkeypatch.setattr(svc.solver_runtime, "async_run_solve", solve)
    monkeypatch.setattr(
        svc.solver_runtime, "record_solve_completed", lambda **kw: recorded.append(kw)
    )
    _call(_hass(), svc.SERVICE_SOLVE_NOW)
    assert recorded == ([{"trigger_source": "solve_now"}] if ok else [])
    assert ("did not produce a successful solve" in caplog.text) is not ok


# --- reports ----------------------------------------------------------------------------


def test_the_flex_record_service_returns_the_record_or_why_there_is_none():
    sw = importlib.import_module("custom_components.nimbus_load.solver_writer")
    eid = sw.FLEX_TELEMETRY_ENTITY_ID
    nothing = _call(_hass(), svc.SERVICE_FLEX_TELEMETRY_RECORD)
    assert nothing == {"record": None, "reason": svc._FLEX_TELEMETRY_NOT_PUBLISHED}
    refused = SimpleNamespace(
        attributes={"reason": "no location", "reason_code": "region_unresolved"}
    )
    out = _call(_hass(states={eid: refused}), svc.SERVICE_FLEX_TELEMETRY_RECORD)
    assert out["record"] is None and out["reason_code"] == "region_unresolved"
    record = {"interval_start_utc": "2026-09-20T06:00:00Z"}
    built = SimpleNamespace(
        attributes={"record": record, "clamped_fields": ("x",), "generated_at": "t"}
    )
    assert _call(_hass(states={eid: built}), svc.SERVICE_FLEX_TELEMETRY_RECORD) == {
        "record": record,
        "interval_start_utc": "2026-09-20T06:00:00Z",
        "clamped_fields": ["x"],
        "generated_at": "t",
    }


@pytest.fixture
def writer(monkeypatch):
    sw = importlib.import_module("custom_components.nimbus_load.solver_writer")
    monkeypatch.setattr(sw, "fetch_solver_config", lambda: {"cfg": 1})
    return sw


def test_a_quality_report_is_scored_for_a_past_window(writer, monkeypatch):
    seen = []
    monkeypatch.setattr(
        writer,
        "_compute_report_for_window",
        lambda cfg, s, e, allow_partial: seen.append(allow_partial) or {"epr": 0.9},
    )
    out = _call(
        _hass(),
        svc.SERVICE_COMPUTE_QUALITY_REPORT,
        {
            "start": "2026-09-19T00:00:00+10:00",
            "end": "2026-09-19T12:00:00+10:00",
            "allow_partial": False,
        },
    )
    assert out["window_hours"] == 12.0 and out["epr"] == 0.9 and seen == [False]


@pytest.mark.parametrize(
    ("start", "end", "raises", "says"),
    [
        (
            "2026-09-19T12:00:00+10:00",
            "2026-09-19T12:00:00+10:00",
            Invalid,
            "strictly after start",
        ),
        (
            "2026-09-19T12:00:00+10:00",
            "2026-09-21T12:00:00+10:00",
            Invalid,
            "must not be in the future",
        ),
        (
            "2026-09-18T00:00:00+10:00",
            "2026-09-19T00:00:00+10:00",
            Failed,
            "cannot score window",
        ),
    ],
)
def test_a_window_that_cannot_be_scored_says_why(
    writer, monkeypatch, start, end, raises, says
):
    monkeypatch.setattr(writer, "_compute_report_for_window", lambda *a, **k: None)
    with pytest.raises(raises, match=says):
        _call(_hass(), svc.SERVICE_COMPUTE_QUALITY_REPORT, {"start": start, "end": end})


def test_a_scoring_failure_is_reported_with_its_window(writer, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no history")

    monkeypatch.setattr(writer, "_compute_report_for_window", boom)
    with pytest.raises(Failed, match="scoring failed .*no history"):
        _call(
            _hass(),
            svc.SERVICE_COMPUTE_QUALITY_REPORT,
            {"start": "2026-09-18T00:00:00+10:00", "end": "2026-09-19T00:00:00+10:00"},
        )


def test_rescoring_takes_a_count_of_days_or_one_complete_past_day(writer, monkeypatch):
    seen = []
    monkeypatch.setattr(
        writer,
        "rescore_quality_history",
        lambda cfg, now, days, only_dates: seen.append((days, only_dates)) or {"ok": 1},
    )
    assert _call(_hass(), svc.SERVICE_RESCORE_HISTORY, {"days": 3}) == {"ok": 1}
    _call(_hass(), svc.SERVICE_RESCORE_HISTORY, {"date": "2026-09-17"})
    assert seen == [(3, None), (3, {"2026-09-17"})]
    for bad, says in (
        ("2026-09-20", "0 day"),
        ("2026-08-01", "50 day"),
        ("17/09/2026", "not an ISO"),
    ):
        with pytest.raises(Invalid, match=says):
            _call(_hass(), svc.SERVICE_RESCORE_HISTORY, {"date": bad})
    with pytest.raises(vol.Invalid):
        _call(_hass(), svc.SERVICE_RESCORE_HISTORY, {"days": 31})


@pytest.mark.parametrize(
    ("error", "raises"),
    [(ValueError("too many days"), Invalid), (RuntimeError("disk"), Failed)],
)
def test_a_rescore_failure_is_a_validation_or_a_plain_error(
    writer, monkeypatch, error, raises
):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(writer, "rescore_quality_history", boom)
    with pytest.raises(raises):
        _call(_hass(), svc.SERVICE_RESCORE_HISTORY, {})


# --- one-hub services ---------------------------------------------------------------------


def test_decision_inputs_export_needs_exactly_one_hub(monkeypatch):
    store = importlib.import_module(
        "custom_components.nimbus_load.decision_inputs_store"
    )

    async def export(hass, entry_id, start, end, kinds):
        if kinds == ["horizon"]:
            raise ValueError("window too long")
        return {"entry": entry_id, "kinds": kinds, "start": start}

    monkeypatch.setattr(store, "async_export", export)
    window = {"start": "2026-09-19T00:00:00+10:00", "end": "2026-09-19T06:00:00+10:00"}
    out = _call(_hass([_hub()]), svc.SERVICE_EXPORT_DECISION_INPUTS, window)
    assert out == {
        "entry": "hub1",
        "kinds": ["head", "horizon"],
        "start": datetime(2026, 9, 18, 14, 0, tzinfo=UTC),
    }
    with pytest.raises(Invalid, match="window too long"):
        _call(
            _hass([_hub()]),
            svc.SERVICE_EXPORT_DECISION_INPUTS,
            {**window, "kinds": "horizon"},
        )
    with pytest.raises(Invalid, match="exactly one Nimbus hub"):
        _call(_hass([_hub(), _hub()]), svc.SERVICE_EXPORT_DECISION_INPUTS, window)


def test_the_device_contract_services_reach_the_one_hub(monkeypatch):
    dc = importlib.import_module("custom_components.nimbus_load.device_contract_ha")
    seen = []

    def rec(name):
        async def f(hass, entry, *args):
            seen.append((name, entry.entry_id, args))
            return {"ok": True}

        return f

    for name in ("async_preview", "async_set_mode", "async_discover", "async_confirm"):
        monkeypatch.setattr(dc, name, rec(name))
    hass = _hass([_hub()])
    _call(hass, svc.SERVICE_DEVICE_CONTRACT_PREVIEW)
    _call(hass, svc.SERVICE_DEVICE_CONTRACT_SET_MODE, {"mode": "shadow"})
    _call(hass, svc.SERVICE_DEVICE_CONTRACT_DISCOVER)
    _call(
        hass,
        svc.SERVICE_DEVICE_CONTRACT_CONFIRM,
        {"revision": "4", "choices": [{"kind": "battery", "device": "d1"}]},
    )
    assert [s[0] for s in seen] == [
        "async_preview",
        "async_set_mode",
        "async_discover",
        "async_confirm",
    ]
    assert seen[-1][2] == (4, [{"kind": "battery", "device": "d1"}])
    with pytest.raises(vol.Invalid):
        _call(hass, svc.SERVICE_DEVICE_CONTRACT_SET_MODE, {"mode": "turbo"})
    with pytest.raises(vol.Invalid):
        _call(hass, svc.SERVICE_DEVICE_CONTRACT_CONFIRM, {"revision": 1, "choices": []})
    with pytest.raises(Invalid, match="exactly one Nimbus hub"):
        _call(_hass(), svc.SERVICE_DEVICE_CONTRACT_PREVIEW)


def test_the_stochastic_summary_is_the_last_shadow_solve(monkeypatch):
    plan = importlib.import_module("custom_components.nimbus_load.solver_plan")
    monkeypatch.setattr(plan, "stochastic_snapshot", lambda: {"paths": 8})
    assert _call(_hass(), svc.SERVICE_GET_STOCHASTIC_SUMMARY) == {"paths": 8}


# --- set_controllable_load -------------------------------------------------------------------

NAME = const.CONF_CONTROLLABLE_LOAD_NAME
KIND = const.CONF_CONTROLLABLE_LOAD_KIND


def _load(sid, title, **data):
    return SimpleNamespace(
        subentry_id=sid,
        subentry_type=const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        title=title,
        data={NAME: title, **data},
    )


def _set(hass, **data):
    return asyncio.run(
        hass.services.handlers[(DOMAIN, svc.SERVICE_SET_CONTROLLABLE_LOAD)](
            SimpleNamespace(data=data)
        )
    )


def test_a_new_load_is_created_with_its_kind_and_the_hub_reloads(monkeypatch):
    monkeypatch.setattr(svc, "ConfigSubentry", lambda **kw: SimpleNamespace(**kw))
    hass = _hass([_hub()])
    out = _set(hass, **{NAME: "Pool pump", KIND: "sheddable"})
    assert out == {
        "created": True,
        "subentry_id": "new1",
        "name": "Pool pump",
        "kind": "sheddable",
        "data": {NAME: "Pool pump", KIND: "sheddable"},
    }
    assert (
        hass.config_entries.added[0].subentry_type
        == const.SUBENTRY_TYPE_CONTROLLABLE_LOAD
    )
    assert hass.config_entries.reloaded == ["hub1"]


def test_an_existing_load_is_updated_in_place_keeping_its_kind():
    hub = _hub(l1=_load("l1", "Hot water", **{KIND: "deferrable", "x": 1}))
    hass = _hass([hub])
    out = _set(hass, **{NAME: "Hot water", "x": None, "y": 2})
    assert out["created"] is False and out["kind"] == "deferrable"
    assert hass.config_entries.updated == [
        ("l1", "Hot water", {NAME: "Hot water", KIND: "deferrable", "y": 2})
    ]
    renamed = _set(hass, subentry_id="l1", **{NAME: "HWS"})
    assert (
        renamed["subentry_id"] == "l1" and hass.config_entries.updated[-1][1] == "HWS"
    )


@pytest.mark.parametrize(
    ("entries", "data", "raises", "says"),
    [
        ([], {NAME: "x"}, Failed, "no Nimbus hub"),
        ([_hub()], {}, Invalid, "'controllable_load_name' is required"),
        ([_hub()], {NAME: "New"}, Invalid, "'controllable_load_kind' is required"),
        (
            [_hub(a=_load("a", "Twin"), b=_load("b", "Twin"))],
            {NAME: "Twin"},
            Invalid,
            r"2 existing .*\(a, b\)",
        ),
        (
            [_hub()],
            {NAME: "x", "subentry_id": "nope"},
            Invalid,
            "no controllable_load subentry",
        ),
    ],
)
def test_a_call_that_cannot_be_applied_changes_nothing(entries, data, raises, says):
    hass = _hass(entries)
    with pytest.raises(raises, match=says):
        _set(hass, **data)
    assert hass.config_entries.updated == [] and hass.config_entries.reloaded == []


def test_the_service_accepts_the_wizards_fields_and_its_own_extras():
    schema = svc.SERVICE_SET_CONTROLLABLE_LOAD_SCHEMA
    assert (
        schema(
            {
                NAME: "x",
                "subentry_id": "a",
                const.CONF_CONTROLLABLE_LOAD_POWER_SENSOR: "sensor.p",
            }
        )["subentry_id"]
        == "a"
    )
    with pytest.raises(vol.Invalid):
        schema({"no_such_field": 1})
