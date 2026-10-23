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


def test_the_config_is_kept_once_and_the_price_sources_series_with_the_horizons(site):
    site.publish(0, 2.0)
    site.capture()
    site.publish(5, 2.0)
    site.capture()
    out = site.export(T0, T0 + timedelta(hours=1))
    (fp,) = out["configs"]
    assert (
        "friendly_name" not in out["configs"][fp]
        and out["configs"][fp]["solver_import_price_sensor"] == "sensor.buy"
    )
    horizon = out["records"][0]
    buy = next(line for line in horizon["sources"] if line["entity_id"] == "sensor.buy")
    series = out["series"][buy["series"]]
    assert (series["to_dollars"], series["value"][:2]) == (0.01, [20, 21])
    assert buy["forecast_rows"] == 4
    entities = [line["entity_id"] for line in horizon["sources"]]
    assert (
        "sensor.load_fc" in entities and "sensor.other_buy" not in entities
    )  # a missing comparison is skipped
    head_sources = out["records"][1]["sources"]
    assert all("series" not in line for line in head_sources)


def test_an_export_can_ask_for_heads_only_or_horizons_only(site):
    site.publish(0, 2.0)
    site.capture()
    site.publish(5, 2.0)
    site.capture()
    heads = site.export(T0, T0 + timedelta(hours=1), ["head"])
    assert heads["count"] == 2 and all(
        len(r["rows"]["time"]) == 4 for r in heads["records"]
    )
    assert heads["records"][0]["trimmed_to_head"] is True
    horizons = site.export(T0, T0 + timedelta(hours=1), ["horizon"])
    assert [r["kind"] for r in horizons["records"]] == ["horizon"]
    assert site.export(T0 + timedelta(hours=2), T0 + timedelta(hours=3))["count"] == 0


@pytest.mark.parametrize(
    ("start", "end", "says"),
    [(T0, T0, "after start"), (T0, T0 + timedelta(hours=25), "at most")],
)
def test_an_export_window_must_be_forward_and_at_most_a_day(site, start, end, says):
    with pytest.raises(ValueError, match=says):
        site.export(start, end)


def test_old_segments_are_pruned_on_flush(site, tmp_path):
    site.publish(0, 2.0)
    site.capture()
    root = store._root(site.hass, "hub1")
    assert store._list_keys(root, "horizon") == [di.segment_key(T0)]
    site.clock = T0 + di.HORIZON_KEEP + timedelta(hours=2)
    site.publish(60 * 24 * 8, 2.0)
    site.capture()
    asyncio.run(store.async_flush(site.hass, "hub1"))
    assert di.segment_key(T0) not in store._list_keys(root, "horizon")


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


@pytest.mark.parametrize(
    ("kw", "cls"),
    [(0.06, "discharge"), (-0.06, "charge"), (0.05, "self_consume"), (None, None)],
)
def test_dispatch_class_uses_the_automations_own_deadband(kw, cls):
    assert di.dispatch_class(kw) == cls


def test_segments_and_windows_are_whole_utc_hours():
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


def test_a_price_array_is_kept_per_field():
    config = {
        di.ARRAY_SENSOR_KEY: "sensor.array",
        "solver_price_forecast_array_import_key": "costsflexup",
    }
    assert di.keyed_price_fields(config) == {
        "sensor.array": ["costsflexup", "earningsflexup"]
    }
    assert di.keyed_price_fields({}) == {}
    attrs = {
        "forecast": [
            {"time": T0.isoformat(), "costsflexup": 30, "earningsflexup": 5},
            {"time": (T0 + timedelta(minutes=5)).isoformat(), "costsflexup": 31},
        ],
        "unit_of_measurement": "$/kWh",
    }
    body = di.source_series(attrs, "costsflexup")
    assert (body["value"], body["step_min"]) == ([30, 31], [0, 5])
    assert di.source_series({}, None) is None
