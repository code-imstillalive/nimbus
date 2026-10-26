"""Decision inputs (#1657): what each solve looked at, kept for replay --
a head of every plan, the whole horizon every half hour and on every change
of dispatch direction, the resolved config once per fingerprint, and each
price source's series -- written to disk, pruned, and exported by window
(#1837).

Driven as Home Assistant drives it: a plan published, captured, flushed and
read back. Covers what lived in #1652, #1657, #1786 and the decision-inputs
files.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

store = importlib.import_module("custom_components.nimbus_load.decision_inputs_store")
di = importlib.import_module("custom_components.nimbus_load.decision_inputs")
shared = importlib.import_module("custom_components.nimbus_load.solver_shared")

T0 = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)  # 04:00 Brisbane


def _rows(battery_kw, n=48):
    return [
        {
            "time": (T0 + timedelta(minutes=30 * i)).isoformat(),
            "hours": 0.5,
            "battery_kw": battery_kw,
            "import_price": 0.2 + i / 100,
            "soc_pct": 50,
        }
        for i in range(n)
    ]


class Site:
    def __init__(self, tmp):
        self.clock = T0
        self.config = {
            "solver_import_price_sensor": "sensor.buy",
            "solver_load_forecast_sensor": "sensor.load_fc",
            "friendly_name": "x",
        }
        self.states = {
            "sensor.nimbus_solver_config": SimpleNamespace(
                state="configured", attributes=self.config, last_updated=T0
            ),
            "sensor.buy": SimpleNamespace(
                state="0.2",
                attributes={
                    "unit_of_measurement": "c/kWh",
                    "forecast": [
                        {
                            "time": (T0 + timedelta(minutes=30 * i)).isoformat(),
                            "value": 20 + i,
                        }
                        for i in range(4)
                    ],
                },
                last_updated=T0,
            ),
            "sensor.load_fc": SimpleNamespace(
                state="1.2",
                attributes={"forecast": [{"time": T0.isoformat(), "value": 1.2}]},
                last_updated=T0,
            ),
        }

        async def executor(fn, *a):
            return fn(*a)

        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get),
            config=SimpleNamespace(path=lambda *p: str(tmp.joinpath(*p))),
            async_add_executor_job=executor,
            config_entries=SimpleNamespace(
                async_get_entry=lambda eid: SimpleNamespace(
                    options={"solver_price_comparison_sensors": "sensor.other_buy"}
                )
            ),
        )

    def publish(self, minutes, battery_kw):
        self.clock = T0 + timedelta(minutes=minutes)
        self.states["sensor.nimbus_solver_battery_forecast"] = SimpleNamespace(
            state="1",
            attributes={
                "forecast": _rows(battery_kw),
                "status": "optimal",
                "nimbus_version": "v",
            },
            last_updated=self.clock,
        )

    def capture(self):
        return asyncio.run(store.async_capture(self.hass, "hub1"))

    def export(self, start, end, kinds=("head", "horizon")):
        return asyncio.run(
            store.async_export(self.hass, "hub1", start, end, list(kinds))
        )


@pytest.fixture
def site(monkeypatch, tmp_path):
    s = Site(tmp_path)
    store.reset_decision_inputs_cache()
    monkeypatch.setattr(store.dt_util, "utcnow", lambda: s.clock)
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    yield s
    store.reset_decision_inputs_cache()


def test_every_solve_keeps_a_head_and_the_whole_horizon_when_it_matters(site):
    kinds = []
    for minutes, kw in (
        (0, 2.0),
        (5, 2.0),
        (10, -1.0),
        (15, -1.0),
        (40, -1.0),
        (45, -1.0),
    ):
        site.publish(minutes, kw)
        kinds.append(site.capture())
    # first; same direction; direction changed; same; 30 minutes on; same
    assert kinds == ["horizon", "head", "horizon", "head", "horizon", "head"]
    site.publish(45, -1.0)
    assert site.capture() is None  # the same plan again: nothing new
    out = site.export(T0, T0 + timedelta(hours=1))
    assert out["count"] == 6
    reasons = [r.get("reason") for r in out["records"]]
    assert reasons == ["first", None, "dispatch_class_change", None, "interval", None]
    head = out["records"][1]
    assert (
        len(head["rows"]["time"]) == int(di.HEAD_HOURS * 2)
        and head["status"] == "optimal"
    )
    assert len(out["records"][0]["rows"]["time"]) == 48


@pytest.mark.parametrize(
    ("start", "end", "says"),
    [(T0, T0, "after start"), (T0, T0 + timedelta(hours=25), "at most")],
)
def test_an_export_window_must_be_forward_and_at_most_a_day(site, start, end, says):
    with pytest.raises(ValueError, match=says):
        site.export(start, end)


def test_nothing_is_captured_without_a_published_plan(site):
    assert site.capture() is None
    site.publish(0, 2.0)
    site.states["sensor.nimbus_solver_battery_forecast"].attributes["forecast"] = []
    assert site.capture() is None


def test_the_plan_this_install_posted_is_preferred(site, monkeypatch):
    monkeypatch.setattr(shared.NATIVE, "hass", site.hass)
    monkeypatch.setitem(
        shared._LAST_POSTED, store._PLAN_ENTITY, (1.0, {"forecast": _rows(0.0, 4)})
    )
    monkeypatch.setitem(shared._LAST_POSTED_AT, store._PLAN_ENTITY, T0)
    assert site.capture() == "horizon"
    out = site.export(T0, T0 + timedelta(hours=1))
    assert len(out["records"][0]["rows"]["time"]) == 4


def test_dispatch_class_uses_the_automations_own_deadband():
    # test_dispatch_class_uses_the_automations_own_deadband
    for kw, cls in [
        (0.06, "discharge"),
        (-0.06, "charge"),
        (0.05, "self_consume"),
        (None, None),
    ]:
        assert di.dispatch_class(kw) == cls

    # test_segments_and_windows_are_whole_utc_hours
    keys = [di.segment_key(T0 + timedelta(hours=h)) for h in range(3)] + ["junk"]
    assert (
        di.segments_overlapping(
            keys, T0 + timedelta(minutes=30), T0 + timedelta(hours=1, minutes=1)
        )
        == keys[:2]
    )
    assert (
        di.expired(
            keys,
            T0 + timedelta(hours=2) + timedelta(days=7, minutes=1),
            timedelta(days=7),
        )
        == keys[:2]
    )
    with pytest.raises(ValueError):
        di.segment_key(datetime(2026, 1, 1))  # noqa: DTZ001
    assert di.in_window({"captured_at": "x"}, T0, T0) is False

    # test_a_providers_series_is_kept_in_its_own_shape_and_reads_back
    for attrs, shape in [
        (lambda: {"forecast": _sample("1550_aemo_nem_qld.json")}, pi.AEMO_NEM_DOMAIN),
        (
            lambda: {"forecast": _sample("1550_pd7day_qld_wholesale.json")},
            pi.PD7DAY_DOMAIN,
        ),
        (
            lambda: {
                "forecast": _sample("1550_amber_core.json", "amber_import_action")
            },
            pi.AMBER_CORE_DOMAIN,
        ),
        (
            lambda: {
                "forecast": [],
                pi.AMBER_CORE_ROWS_KEY: _sample(
                    "1550_amber_core.json", "amber_import_action"
                ),
            },
            pi.AMBER_CORE_DOMAIN,
        ),
        (_express, pi.AMBER_EXPRESS_DOMAIN),
    ]:
        a = attrs()
        body = di.source_series(a)
        assert (body["shape"], body["to_dollars"]) == (shape, 1.0)
        shape_again, intervals = di._provider_series(a)
        assert shape_again == shape
        stored = json.loads(json.dumps(body))
        assert di.series_points(stored) == [(i.start, i.value) for i in intervals]
        assert body["end"] == intervals[-1].end.isoformat()

    # test_start_end_rows_keep_their_end_and_drop_what_cannot_be_read
    rows = [
        {
            "start": (T0 + timedelta(minutes=5 * i)).isoformat(),
            "end": (T0 + timedelta(minutes=5 * i + 5)).isoformat(),
            "value": v,
        }
        for i, v in enumerate([0.1, "x", 0.3])
    ]
    rows.append({"start": T0.isoformat(), "end": T0.isoformat(), "value": 1})
    body = di.source_series({"forecast": rows})
    assert (body["shape"], body["value"], body["step_min"]) == (
        di.SHAPE_START_END,
        [0.1, 0.3],
        [0, 10],
    )
    assert body["end"] == (T0 + timedelta(minutes=15)).isoformat()


# --- each source as published -----------------------------------------------------------------

pi = importlib.import_module("custom_components.nimbus_load.price_intervals")
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pricing"


def _sample(name, sample=None):
    d = json.loads((FIX / name).read_text(encoding="utf-8"))
    return (d["samples"][sample] if sample else d)["selected_native_rows"]


def _express():
    return {
        "forecast": _sample("1550_amber_express.json", "amber_express_general_simple"),
        pi.AMBER_EXPRESS_DETAILED_KEY: _sample(
            "1550_amber_express.json", "amber_express_general_detail"
        ),
    }


def _array_rows(n=12):
    return [
        {
            "time": (T0 + timedelta(minutes=5 * i)).isoformat(),
            "costsflexup": 0.2 + i / 100,
            "earningsflexup": 0.05,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("comparison", [["sensor.cmp_aemo"], []])
def test_a_horizon_keeps_the_array_per_key_and_each_comparison_source(site, comparison):
    site.config[di.ARRAY_SENSOR_KEY] = "sensor.array"
    site.states["sensor.array"] = SimpleNamespace(
        state="0.2",
        attributes={"forecast": _array_rows(), "unit_of_measurement": "$/kWh"},
        last_updated=T0,
    )
    site.states["sensor.cmp_aemo"] = SimpleNamespace(
        state="0.1",
        attributes={"forecast": _sample("1550_aemo_nem_qld.json")},
        last_updated=T0,
    )
    site.hass.config_entries = SimpleNamespace(
        async_get_entry=lambda eid: SimpleNamespace(
            options={"solver_price_comparison_sensors": comparison}
        )
    )
    site.publish(0, 2.0)
    site.capture()
    site.publish(5, 2.0)
    site.capture()
    out = site.export(T0, T0 + timedelta(hours=1))
    horizon, head = (
        {line["entity_id"]: line for line in r["sources"]} for r in out["records"]
    )
    array = horizon["sensor.array"]
    assert "series" not in array and set(array["series_fields"]) == {
        "costsflexup",
        "earningsflexup",
    }
    for key, fp in array["series_fields"].items():
        assert out["series"][fp]["value"] == [row[key] for row in _array_rows()]
    if comparison:
        cmp_line = horizon["sensor.cmp_aemo"]
        assert out["series"][cmp_line["series"]]["shape"] == pi.AEMO_NEM_DOMAIN
    else:
        assert "sensor.cmp_aemo" not in horizon
    assert "sensor.cmp_aemo" not in head
    refs = {
        fp
        for r in out["records"]
        for line in r["sources"]
        for fp in [line.get("series"), *(line.get("series_fields") or {}).values()]
        if fp
    }
    assert refs == set(out["series"])
