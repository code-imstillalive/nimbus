"""nimbus #1786 step 1: a horizon keeps each price source's own forecast series.

#1676's capture kept only a summary of each source (value, `last_updated`,
row count, first and last time), so a consolidation rule other than the one
the Solver used could not be scored afterwards. Synthetic data only, no
household telemetry. The store runs against real files in a temp dir, through
the same stand-in `hass` as `test_1657_decision_inputs.py`.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import decision_inputs as di
from custom_components.nimbus_load import decision_inputs_store as store

T0 = datetime(2026, 10, 9, 4, 0, tzinfo=UTC)

CONFIG = {
    "friendly_name": "Nimbus Solver Config",
    "solver_import_price_sensor": "sensor.lv_buy",
    "solver_export_price_sensor": "sensor.lv_sell",
    "solver_import_price_sensor_2": "sensor.amber_general",
    "solver_export_price_sensor_2": "sensor.amber_feed_in",
    "solver_import_price_sensor_3": "sensor.amber_core_general",
    "solver_regional_spot_forecast_sensor": "sensor.pd7day",
    "solver_regional_spot_current_price_sensor": "sensor.aemo_nem",
    "solver_solar_forecast_sensor": "sensor.solcast",  # not a price input
    "solver_load_forecast_sensor": "sensor.load_fc",  # not a price input
    "solver_battery_soc_sensor": "sensor.soc",  # not an input line at all
}


def _iso(t: datetime) -> str:
    return t.isoformat()


def _floor(t: datetime, minutes: int) -> datetime:
    return t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % minutes)


def lv_attrs(now: datetime, rng: random.Random, n: int = 288) -> dict:
    """LocalVolts v2 Flex Up as the Solver reads it: generic `{time, value}`
    rows in c/kWh."""
    s = _floor(now, 5)
    return {
        "unit_of_measurement": "c/kWh",
        "forecast": [
            {
                "time": _iso(s + timedelta(minutes=5 * i)),
                "value": round(rng.uniform(-5, 60), 2),
            }
            for i in range(n)
        ],
    }


def amber_express_attrs(now: datetime, rng: random.Random) -> dict:
    """12 five-minute then 46 thirty-minute rows: `forecast` carries the
    price, `detailedForecast` the boundaries (start one second in)."""
    rows, detailed, t = [], [], _floor(now, 5)
    for i in range(58):
        if i == 12:
            t = _floor(t, 30) + timedelta(minutes=30) if t.minute % 30 else t
        d = 5 if i < 12 else 30
        value = round(rng.uniform(0, 0.6), 5)
        rows.append({"time": _iso(t), "value": value})
        detailed.append(
            {
                "start_time": _iso(t + timedelta(seconds=1)),
                "end_time": _iso(t + timedelta(minutes=d)),
                "duration": d,
                "per_kwh": value + 1.0,  # not the published price: never read
                "spot_per_kwh": 0.05,
            }
        )
        t += timedelta(minutes=d)
    return {
        "unit_of_measurement": "$/kWh",
        "forecast": rows,
        "detailedForecast": detailed,
    }


def amber_core_attrs(now: datetime) -> dict:
    s = _floor(now, 30)
    return {
        "unit_of_measurement": "$/kWh",
        "forecasts": [
            {
                "start_time": _iso(s + timedelta(minutes=30 * i, seconds=1)),
                "end_time": _iso(s + timedelta(minutes=30 * (i + 1))),
                "duration": 30,
                "per_kwh": 0.2 + i / 100,
                "spot_per_kwh": 0.05,
            }
            for i in range(4)
        ],
    }


def pd7day_attrs(now: datetime, rng: random.Random, n: int = 336) -> dict:
    s = _floor(now, 30)
    return {
        "unit_of_measurement": "$/MWh",  # the state's basis, not the rows'
        "forecast": [
            {
                "time": _iso(s + timedelta(minutes=30 * i)),
                "nemtime": _iso(s + timedelta(minutes=30 * (i + 1))),
                "value": round(rng.uniform(0, 0.3), 5),
                "raw_value": 9.9,
                "calibrated": 9.9,
            }
            for i in range(n)
        ],
    }


def aemo_nem_attrs(now: datetime) -> dict:
    s = _floor(now, 30)
    return {
        "unit_of_measurement": "$/kWh",
        "forecast": [
            {
                "start_time": _iso(s + timedelta(minutes=30 * i)),
                "end_time": _iso(s + timedelta(minutes=30 * (i + 1))),
                "price": -0.01 * i,
            }
            for i in range(3)
        ],
    }


def p5min_attrs(now: datetime, rng: random.Random) -> dict:
    s = _floor(now, 5)
    return {
        "unit_of_measurement": "AUD/kWh",
        "forecast": [
            {
                "start": _iso(s + timedelta(minutes=5 * i)),
                "end": _iso(s + timedelta(minutes=5 * (i + 1))),
                "value": round(rng.uniform(0, 0.3), 6),
            }
            for i in range(12)
        ],
    }


# ---- pure: one series per source shape --------------------------------------


def test_generic_rows_are_kept_as_published_with_their_unit():
    attrs = lv_attrs(T0, random.Random(1), n=3)
    body = di.source_series(attrs)
    assert body["shape"] == di.SHAPE_GENERIC
    assert body["unit"] == "c/kWh" and body["to_dollars"] == pytest.approx(0.01)
    assert body["value"] == [r["value"] for r in attrs["forecast"]]  # cents, raw
    assert body["t0"] == _iso(T0) and body["step_min"] == [0, 5, 5]
    assert body["end"] is None  # a step row states no end


def test_generic_rows_fall_back_to_calibrated_as_the_solver_does():
    body = di.source_series({"forecast": [{"time": _iso(T0), "calibrated": 0.31}]})
    assert body["value"] == [0.31] and body["to_dollars"] == 1.0


def test_pd7day_keeps_the_published_value_never_raw_value():
    attrs = pd7day_attrs(T0, random.Random(2), n=2)
    body = di.source_series(attrs)
    assert body["shape"] == "nem_pd7day"
    assert body["value"] == [r["value"] for r in attrs["forecast"]]
    assert body["step_min"] == [0, 30]
    assert body["end"] == _iso(T0 + timedelta(minutes=60))
    # The rows are $/kWh by the adapter's contract, whatever the state's unit.
    assert body["unit"] == "$/MWh" and body["to_dollars"] == 1.0


def test_aemo_nem_keeps_negative_prices():
    body = di.source_series(aemo_nem_attrs(T0))
    assert body["shape"] == "aemo_nem"
    assert body["value"] == [0.0, -0.01, -0.02]


def test_amber_electric_core_forecasts_on_canonical_boundaries():
    body = di.source_series(amber_core_attrs(T0))
    assert body["shape"] == "amberelectric"
    assert body["t0"] == _iso(T0)  # not 04:00:01
    assert body["value"] == pytest.approx([0.2, 0.21, 0.22, 0.23])
    assert body["end"] == _iso(T0 + timedelta(hours=2))


def test_amber_express_reads_forecast_value_on_detailed_boundaries():
    attrs = amber_express_attrs(T0, random.Random(3))
    body = di.source_series(attrs)
    assert body["shape"] == "amber_express"
    assert body["value"] == [r["value"] for r in attrs["forecast"]]  # not per_kwh
    points = di.series_points(body)
    assert [p[0] for p in points] == [
        datetime.fromisoformat(r["time"]) for r in attrs["forecast"]
    ]
    assert body["step_min"][:3] == [0, 5, 5] and body["step_min"][12] == 5


def test_p5min_start_end_rows():
    attrs = p5min_attrs(T0, random.Random(4))
    body = di.source_series(attrs)
    assert body["shape"] == di.SHAPE_START_END
    assert len(body["value"]) == 12
    assert body["end"] == _iso(T0 + timedelta(hours=1))


def test_a_source_with_no_readable_forecast_has_no_series():
    assert di.source_series({}) is None
    assert di.source_series({"forecast": [{"time": "nope", "value": 1}]}) is None
    # OpenADR's forecast exists only behind its action: not fetched here.
    assert (
        di.source_series(
            {"payload_type": "PRICE", "forecast_rows": 3, "forecast_start": _iso(T0)}
        )
        is None
    )


def test_series_points_round_trips_a_body():
    attrs = amber_express_attrs(T0, random.Random(5))
    body = json.loads(json.dumps(di.source_series(attrs)))
    points = di.series_points(body)
    assert points[0] == (T0, attrs["forecast"][0]["value"])
    assert [v for _t, v in points] == body["value"]


def test_price_sources_are_the_price_keys_plus_p5min():
    assert di.price_source_entities(CONFIG) == [
        "sensor.lv_sell",
        "sensor.amber_feed_in",
        "sensor.lv_buy",
        "sensor.amber_general",
        "sensor.amber_core_general",
        "sensor.aemo_nem",
        "sensor.pd7day",
        di.P5MIN_ENTITY,
    ]
    assert set(di.price_source_entities(CONFIG)) - {di.P5MIN_ENTITY} <= set(
        di.source_entities(CONFIG)
    )


# ---- the store ------------------------------------------------------------------


class _Hass:
    def __init__(self, root: Path):
        self.states_by_id: dict[str, SimpleNamespace] = {}
        self.config = SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts)))
        self.states = SimpleNamespace(get=self.states_by_id.get)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def set(self, entity_id: str, state, attributes: dict, updated: datetime):
        self.states_by_id[entity_id] = SimpleNamespace(
            state=state, attributes=attributes, last_updated=updated
        )


def _plan(start: datetime, battery_now: float, rng: random.Random) -> dict:
    rows, t = [], start
    for i in range(202):
        hours = 5 / 60 if i < 48 else 0.5
        rows.append(
            {
                "time": _iso(t),
                "hours": round(hours, 4),
                "battery_kw": battery_now if i == 0 else round(rng.uniform(-40, 40), 3),
                "import_price": round(rng.uniform(0, 0.6), 4),
                "import_price_raw": round(rng.uniform(0, 0.6), 4),
                "import_price_source": "primary",
                "export_price": round(rng.uniform(0, 0.6), 4),
                "export_price_raw": round(rng.uniform(0, 0.6), 4),
                "export_price_source": "primary",
                "load_kw": round(rng.uniform(0, 5), 3),
                "solar_kw": round(rng.uniform(0, 40), 3),
                "soc_pct": round(rng.uniform(10, 100), 2),
                "shadow_price": round(rng.uniform(0, 0.4), 4),
            }
        )
        t += timedelta(hours=hours)
    return {"forecast": rows, "generated_at": _iso(start), "status": "optimal"}


_SOURCES = (
    ("sensor.lv_buy", lambda now, rng: lv_attrs(now, rng), 5),
    ("sensor.lv_sell", lambda now, rng: lv_attrs(now, rng), 5),
    ("sensor.amber_general", amber_express_attrs, 5),
    ("sensor.amber_feed_in", amber_express_attrs, 5),
    ("sensor.amber_core_general", lambda now, _rng: amber_core_attrs(now), 30),
    ("sensor.pd7day", pd7day_attrs, 30),
    ("sensor.aemo_nem", lambda now, _rng: aemo_nem_attrs(now), 30),
    (di.P5MIN_ENTITY, p5min_attrs, 5),
)


class _Rig:
    """Solves at a fixed cadence, every source refreshing on its own."""

    def __init__(self, tmp: Path, clock: dict, seed: int = 1786):
        self.hass = _Hass(tmp)
        self.clock = clock
        self.root = tmp / ".storage" / "nimbus_load_E_decision_inputs"
        self.rng = random.Random(seed)
        self.slots: dict[str, int] = {}
        self.hass.set("sensor.nimbus_solver_config", "configured", dict(CONFIG), T0)
        for eid in ("sensor.solcast", "sensor.load_fc"):
            self.hass.set(eid, "1", {"forecast": [{"time": _iso(T0), "value": 1}]}, T0)

    def refresh(self, now: datetime) -> None:
        for eid, build, every in _SOURCES:
            slot = int((now - T0) / timedelta(minutes=every))
            if self.slots.get(eid) != slot:
                self.slots[eid] = slot
                self.hass.set(eid, "0.1", build(now, self.rng), now)

    def solve(self, at: datetime, battery_now: float = 3.0) -> str | None:
        self.clock["now"] = at
        self.refresh(at)
        self.hass.set(
            "sensor.nimbus_solver_battery_forecast",
            battery_now,
            _plan(at, battery_now, self.rng),
            at,
        )
        return asyncio.run(store.async_capture(self.hass, "E"))

    def flush(self) -> None:
        asyncio.run(store.async_flush(self.hass, "E"))

    def files(self, kind: str) -> list[Path]:
        return sorted((self.root / kind).glob("*.json.gz"))

    def read(self, kind: str) -> list[dict]:
        out = []
        for path in self.files(kind):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                out.append(json.load(fh))
        return out


@pytest.fixture
def rig(tmp_path, monkeypatch):
    store.reset_decision_inputs_cache()
    clock = {"now": T0}
    monkeypatch.setattr(store, "dt_util", SimpleNamespace(utcnow=lambda: clock["now"]))
    yield _Rig(tmp_path, clock)
    store.reset_decision_inputs_cache()


_SHAPES = {
    "sensor.lv_buy": di.SHAPE_GENERIC,
    "sensor.lv_sell": di.SHAPE_GENERIC,
    "sensor.amber_general": "amber_express",
    "sensor.amber_feed_in": "amber_express",
    "sensor.amber_core_general": "amberelectric",
    "sensor.pd7day": "nem_pd7day",
    "sensor.aemo_nem": "aemo_nem",
    di.P5MIN_ENTITY: di.SHAPE_START_END,
}


def test_a_horizon_stores_every_price_sources_series_and_a_head_none(rig):
    assert rig.solve(T0) == "horizon"
    assert rig.solve(T0 + timedelta(minutes=1)) == "head"
    rig.flush()
    (horizon_file,) = rig.read("horizon")
    rec = horizon_file["records"][0]
    lines = {line["entity_id"]: line for line in rec["sources"]}
    for entity_id, shape in _SHAPES.items():
        body = horizon_file["series"][lines[entity_id]["series"]]
        assert body["shape"] == shape, entity_id
        assert (
            body["value"]
            == di.source_series(dict(rig.hass.states_by_id[entity_id].attributes))[
                "value"
            ]
        )
    # Solar and load keep their provenance line and nothing more.
    assert "series" not in lines["sensor.solcast"]
    assert "series" not in lines["sensor.load_fc"]
    # The summary fields forecast_bias.py reads are unchanged.
    assert lines["sensor.lv_buy"]["forecast_rows"] == 288
    assert lines["sensor.lv_buy"]["forecast_last"] is not None
    (head_file,) = rig.read("head")
    head = head_file["records"][0]
    assert all("series" not in line for line in head["sources"])
    assert di.P5MIN_ENTITY not in {line["entity_id"] for line in head["sources"]}
    assert "series" not in head_file  # #1676's file shape, unchanged


def test_p5min_is_left_out_where_the_install_does_not_publish_it(rig):
    rig.slots[di.P5MIN_ENTITY] = 0  # the rig never publishes it
    rig.solve(T0)
    assert di.P5MIN_ENTITY not in rig.hass.states_by_id
    rig.flush()
    rec = rig.read("horizon")[0]["records"][0]
    assert di.P5MIN_ENTITY not in {line["entity_id"] for line in rec["sources"]}


def test_an_unchanged_source_is_stored_once_and_referenced(rig):
    # Two horizons 30 minutes apart, both inside one hour: the 30-minute
    # sources have not refreshed, so both records name the same body.
    rig.solve(T0 + timedelta(minutes=1))  # first
    rig.solve(T0 + timedelta(minutes=29), battery_now=-3.0)  # class change
    rig.flush()
    (data,) = rig.read("horizon")
    first, second = (
        {line["entity_id"]: line.get("series") for line in r["sources"]}
        for r in data["records"]
    )
    assert first["sensor.pd7day"] == second["sensor.pd7day"]
    assert first["sensor.aemo_nem"] == second["sensor.aemo_nem"]
    assert first["sensor.lv_buy"] != second["sensor.lv_buy"]  # refreshed
    refs = [v for d in (first, second) for v in d.values() if v]
    assert set(data["series"]) == set(refs)  # each body once, none orphaned


def test_an_unchanged_source_is_not_re_read(rig, monkeypatch):
    calls: list[str] = []
    real = di.source_series

    def counting(attrs):
        calls.append("x")
        return real(attrs)

    monkeypatch.setattr(di, "source_series", counting)
    rig.solve(T0 + timedelta(minutes=1))
    n_first = len(calls)
    rig.solve(T0 + timedelta(minutes=2), battery_now=-3.0)  # no source refreshed
    assert n_first == len(_SHAPES) and len(calls) == n_first


def test_each_hourly_file_carries_the_bodies_its_records_name(rig):
    rig.solve(T0 + timedelta(minutes=50))
    rig.solve(
        T0 + timedelta(minutes=61), battery_now=-3.0
    )  # next hour, no refresh of pd7day
    rig.flush()
    for data in rig.read("horizon"):
        for rec in data["records"]:
            for line in rec["sources"]:
                if "series" in line:
                    assert line["series"] in data["series"]


def test_export_resolves_every_reference(rig):
    for m in range(0, 60, 1):
        rig.solve(T0 + timedelta(minutes=m), battery_now=3.0 if m < 40 else -3.0)
    for kinds in (["head", "horizon"], ["horizon"], ["head"]):
        out = asyncio.run(
            store.async_export(rig.hass, "E", T0, T0 + timedelta(hours=1), kinds)
        )
        out = json.loads(json.dumps(out, default=str))
        refs = {
            line["series"]
            for r in out["records"]
            for line in r["sources"]
            if "series" in line
        }
        assert refs and refs == set(out["series"]), kinds
        for ref in refs:
            assert di.series_points(out["series"][ref])
    horizons = asyncio.run(
        store.async_export(rig.hass, "E", T0, T0 + timedelta(hours=1), ["horizon"])
    )
    assert [r["reason"] for r in horizons["records"]] == [
        "first",
        "interval",
        "dispatch_class_change",
    ]


def test_retention_is_unchanged_and_prunes_series_with_its_file(rig):
    assert di.KEEP == {"head": timedelta(hours=48), "horizon": timedelta(days=7)}
    rig.solve(T0)
    rig.flush()
    rig.solve(T0 + timedelta(days=8))
    rig.flush()
    files = rig.files("horizon")
    assert [p.name for p in files] == [
        di.segment_key(T0 + timedelta(days=8)) + ".json.gz"
    ]


def test_a_file_written_before_1786_still_reads_and_exports(rig):
    """The #1676 file shape: no `series` key, no `series` on any line."""
    rig.solve(T0)
    rig.flush()
    store.reset_decision_inputs_cache()
    (path,) = rig.files("horizon")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    del data["series"]
    for rec in data["records"]:
        for line in rec["sources"]:
            line.pop("series", None)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    out = asyncio.run(
        store.async_export(rig.hass, "E", T0, T0 + timedelta(hours=1), ["horizon"])
    )
    assert out["count"] == 1 and out["series"] == {}
    assert set(out) >= {
        "start",
        "end",
        "kinds",
        "count",
        "records",
        "configs",
        "retention",
    }
    # A restart appending to that old file adds to it, and its new horizon
    # carries series.
    rig.solve(T0 + timedelta(minutes=31), battery_now=-3.0)
    rig.flush()
    (data,) = rig.read("horizon")
    assert len(data["records"]) == 2 and data["series"]


# ---- size against #1657's budget ------------------------------------------------


def _hour(tmp: Path, monkeypatch, with_series: bool) -> dict[str, tuple[int, int]]:
    """One hour at the 15 s re-plan cadence (4 solves a minute) with eight
    price sources refreshing every 5 or 30 minutes, and the plan changing
    direction twice. Values are jittered so gzip cannot exploit repeats real
    data does not have. Returns {kind: (bytes on disk, records)}."""
    store.reset_decision_inputs_cache()
    clock = {"now": T0}
    monkeypatch.setattr(store, "dt_util", SimpleNamespace(utcnow=lambda: clock["now"]))
    if not with_series:
        monkeypatch.setattr(di, "source_series", lambda _attrs: None)
    rig = _Rig(tmp, clock)
    for i in range(240):
        rig.solve(T0 + timedelta(seconds=15 * i), 3.0 if 80 <= i < 160 else -3.0)
    rig.flush()
    monkeypatch.undo()
    out = {}
    for kind in ("head", "horizon"):
        (path,) = rig.files(kind)
        out[kind] = (path.stat().st_size, len(rig.read(kind)[0]["records"]))
    return out


def test_a_horizon_records_series_cost_stays_inside_the_1657_budget(
    tmp_path, monkeypatch
):
    """Measured here (synthetic, jittered, so an upper bound): eight price
    sources that have all refreshed add about 3.2 KB to a horizon record,
    against about 6.2 KB for the record itself; heads do not move by a byte.
    At 48 interval horizons plus ~50 class changes a day, that is about
    2.2 MB a week at this bound (1.1 MB at 48 a day)."""
    with_ = _hour(tmp_path / "with", monkeypatch, with_series=True)
    without = _hour(tmp_path / "without", monkeypatch, with_series=False)
    assert with_["head"] == without["head"]  # heads are untouched
    n = with_["horizon"][1]
    assert n == without["horizon"][1] == 3  # first, then two class changes
    extra_per_record = (with_["horizon"][0] - without["horizon"][0]) / n
    assert 0 < extra_per_record < 4_000, extra_per_record
    week = extra_per_record * (48 + 50) * 7
    assert week < 2.75e6, week


# ---- scripts/forecast_bias.py (#1783) reads the new export unchanged ----------


def _load_forecast_bias() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "forecast_bias_1786", REPO / "scripts" / "forecast_bias.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["forecast_bias_1786"] = module
    spec.loader.exec_module(module)
    return module


def test_forecast_bias_reads_an_export_with_series_exactly_as_one_without(
    rig, tmp_path
):
    fb = _load_forecast_bias()
    for m in range(60):
        rig.solve(T0 + timedelta(minutes=m, seconds=10), 3.0 if m < 40 else -3.0)
    new = json.loads(
        json.dumps(
            asyncio.run(
                store.async_export(
                    rig.hass, "E", T0, T0 + timedelta(hours=1), ["head", "horizon"]
                )
            ),
            default=str,
        )
    )
    assert new["series"]
    old = json.loads(json.dumps(new))
    del old["series"]
    for rec in old["records"]:
        for line in rec["sources"]:
            line.pop("series", None)
        rec["sources"] = [
            line for line in rec["sources"] if line["entity_id"] != di.P5MIN_ENTITY
        ]
    reports = []
    for name, export in (("new", new), ("old", old)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(export), encoding="utf-8")
        args = fb.parser().parse_args(["export", str(path), "--tz", "UTC"])
        reports.append(fb.run_export(args))
    assert reports[0]["sides"]["import"]["counts"]["observations"] == 60
    assert reports[0]["sides"] == reports[1]["sides"]
