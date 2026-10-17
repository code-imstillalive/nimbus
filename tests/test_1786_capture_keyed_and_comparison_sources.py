"""nimbus #1786 step 1, completed: two gaps measured on devhub on 18 Oct 2026.

1. The price forecast array's rows are `{time, costsflexup, earningsflexup}`.
   No series reader took them (the generic one wants `value`), so the array
   the Solver prices from was never kept.
2. Amber and AEMO's 30-minute pre-dispatch, the two sources #1786's first
   offline scoring found mattered most, are not Solver inputs on that
   install, so the capture never saw them. `solver_price_comparison_sensors`
   records sources for comparison only; the solve never reads it.

Synthetic data only. Reuses the rig in `test_1786_decision_input_source_series`.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_1786_decision_input_source_series as base

di = base.di
store = base.store
T0 = base.T0
REPO = base.REPO

ARRAY = "sensor.lv_flex_up_forecast"
CMP_AMBER = "sensor.cmp_amber_feed_in"
CMP_AEMO = "sensor.cmp_aemo_30min"


def array_attrs(now, rng, n: int = 288) -> dict:
    t0 = base._floor(now, 5)
    rows = [
        {
            "time": base._iso(t0 + timedelta(minutes=5 * i)),
            "costsflexup": round(rng.uniform(0.1, 0.4), 6),
            "earningsflexup": round(rng.uniform(0.0, 0.2), 6),
            "quality": "Fcst",
        }
        for i in range(n)
    ]
    return {"forecast": rows, "unit_of_measurement": "$/kWh"}


def test_keyed_fields_default_to_the_solvers_own_keys():
    assert di.keyed_price_fields({di.ARRAY_SENSOR_KEY: ARRAY}) == {
        ARRAY: ["costsflexup", "earningsflexup"]
    }
    configured = {
        di.ARRAY_SENSOR_KEY: ARRAY,
        "solver_price_forecast_array_import_key": "buy",
        "solver_price_forecast_array_export_key": "sell",
    }
    assert di.keyed_price_fields(configured) == {ARRAY: ["buy", "sell"]}
    assert di.keyed_price_fields({}) == {}


def test_the_array_rows_matched_no_reader_and_now_read_per_key():
    attrs = array_attrs(T0, random.Random(1))
    assert di.source_series(attrs) is None  # the gap, as measured
    for key in ("costsflexup", "earningsflexup"):
        body = di.source_series(attrs, key)
        assert body["shape"] == di.SHAPE_GENERIC
        assert body["value"] == [row[key] for row in attrs["forecast"]]
        assert di.series_points(json.loads(json.dumps(body)))[0][0] == base._floor(
            T0, 5
        )


def test_comparison_sources_follow_the_config_and_p5min():
    out = di.price_source_entities(base.CONFIG, [CMP_AMBER, "sensor.lv_buy", 5])
    assert out[-2:] == [di.P5MIN_ENTITY, CMP_AMBER]
    assert out.count("sensor.lv_buy") == 1


class _Entries:
    def __init__(self, options: dict):
        self.options = options

    def async_get_entry(self, entry_id):
        return SimpleNamespace(options=self.options) if entry_id == "E" else None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    store.reset_decision_inputs_cache()
    clock = {"now": T0}
    monkeypatch.setattr(store, "dt_util", SimpleNamespace(utcnow=lambda: clock["now"]))
    r = base._Rig(tmp_path, clock)
    config = dict(base.CONFIG, **{di.ARRAY_SENSOR_KEY: ARRAY})
    r.hass.set("sensor.nimbus_solver_config", "configured", config, T0)
    r.hass.config_entries = _Entries(
        {"solver_price_comparison_sensors": [CMP_AMBER, CMP_AEMO]}
    )
    extra = (
        (ARRAY, array_attrs, 5),
        (CMP_AMBER, base.amber_express_attrs, 5),
        (CMP_AEMO, lambda now, _rng: base.aemo_nem_attrs(now), 30),
    )
    real_refresh = r.refresh

    def refresh(now):
        real_refresh(now)
        for eid, build, every in extra:
            slot = int((now - T0) / timedelta(minutes=every))
            if r.slots.get(eid) != slot:
                r.slots[eid] = slot
                r.hass.set(eid, "0.1", build(now, r.rng), now)

    r.refresh = refresh
    yield r
    store.reset_decision_inputs_cache()


def _lines(record: dict) -> dict:
    return {line["entity_id"]: line for line in record["sources"]}


def test_a_horizon_keeps_the_array_per_key(rig):
    rig.solve(T0)
    rig.flush()
    (data,) = rig.read("horizon")
    line = _lines(data["records"][0])[ARRAY]
    assert "series" not in line
    rows = rig.hass.states_by_id[ARRAY].attributes["forecast"]
    for key, fp in line["series_fields"].items():
        assert data["series"][fp]["value"] == [row[key] for row in rows]
    assert set(line["series_fields"]) == {"costsflexup", "earningsflexup"}


def test_a_horizon_keeps_each_comparison_source_and_a_head_does_not(rig):
    assert rig.solve(T0) == "horizon"
    assert rig.solve(T0 + timedelta(minutes=1)) == "head"
    rig.flush()
    (data,) = rig.read("horizon")
    lines = _lines(data["records"][0])
    assert data["series"][lines[CMP_AMBER]["series"]]["shape"] == "amber_express"
    assert data["series"][lines[CMP_AEMO]["series"]]["shape"] == "aemo_nem"
    (head,) = rig.read("head")
    assert not {CMP_AMBER, CMP_AEMO} & set(_lines(head["records"][0]))


def test_a_comparison_source_the_install_lacks_is_left_out(rig):
    rig.hass.config_entries = _Entries(
        {"solver_price_comparison_sensors": ["sensor.not_here"]}
    )
    rig.solve(T0)
    rig.flush()
    assert "sensor.not_here" not in _lines(rig.read("horizon")[0]["records"][0])


def test_no_comparison_list_records_exactly_what_it_did(rig):
    rig.hass.config_entries = _Entries({})
    rig.solve(T0)
    rig.flush()
    lines = _lines(rig.read("horizon")[0]["records"][0])
    assert not {CMP_AMBER, CMP_AEMO} & set(lines)


def test_export_resolves_series_fields_too(rig):
    for m in range(40):
        rig.solve(T0 + timedelta(minutes=m))
    out = asyncio.run(
        store.async_export(rig.hass, "E", T0, T0 + timedelta(hours=1), ["horizon"])
    )
    refs = {
        fp
        for r in out["records"]
        for line in r["sources"]
        for fp in [line.get("series"), *(line.get("series_fields") or {}).values()]
        if fp
    }
    assert refs == set(out["series"])
    assert any("series_fields" in line for r in out["records"] for line in r["sources"])


def test_the_solve_never_reads_the_comparison_list():
    """No Solver module names the field. The bridge sensor exposes it like
    every wizard key, so it reaches the solve's config, unread."""
    pkg = REPO / "custom_components" / "nimbus_load"
    readers = sorted(
        p.relative_to(pkg).as_posix()
        for p in pkg.rglob("*.py")
        if re.search(
            r"CONF_SOLVER_PRICE_COMPARISON_SENSORS|solver_price_comparison_sensors",
            p.read_text(encoding="utf-8"),
        )
    )
    assert readers == [
        "const.py",
        "decision_inputs.py",
        "decision_inputs_store.py",
        "flows/hub_options.py",
        "sensor.py",
    ]
    assert not [r for r in readers if r.startswith("solver")]
