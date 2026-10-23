"""nimbus #1657 gate 2: decision-time inputs captured for replay.

Synthetic plans only (no household telemetry). The store is exercised against
a real temporary directory with a stand-in `hass` whose executor runs inline,
so every byte written and read is the real file format.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import decision_inputs as di
from custom_components.nimbus_load import decision_inputs_store as store

T0 = datetime(2026, 10, 9, 4, 0, tzinfo=UTC)


def _plan(start: datetime, battery_now: float, n5: int = 48, n30: int = 154) -> dict:
    """A tiered plan shaped like the published one: 5-minute rows, then
    30-minute rows, every field the publisher writes plus extras that must
    be dropped."""
    rows = []
    t = start
    for i in range(n5 + n30):
        hours = 5 / 60 if i < n5 else 0.5
        rows.append(
            {
                "time": t.isoformat(),
                "hours": round(hours, 4),
                "battery_kw": battery_now if i == 0 else 1.0,
                "soc_pct": 50.0,
                "grid_import_kw": 0.0,
                "grid_export_kw": 0.0,
                "export_bonus_kw": 0.0,
                "envelope_import_limit_kw": 42.0,
                "envelope_export_limit_kw": 40.0,
                "shadow_price": 0.2,
                "import_price": 0.3 + i * 1e-4,
                "import_price_raw": 0.3,
                "import_price_source": "primary",
                "export_price": 0.1,
                "export_price_raw": 0.1,
                "export_price_source": "primary",
                "bonus_price": 0.0,
                "load_kw": 1.5,
                "solar_kw": 0.0,
                "dispatch_direction": "idle",
                "flow_pv_to_load_kw": 0.0,  # derivable; must not be kept
                "savings_pv": 0.0,
            }
        )
        t += timedelta(hours=hours)
    return {
        "forecast": rows,
        "generated_at": start.isoformat(),
        "nimbus_version": "0.0.test",
        "status": "optimal",
        "solve_seconds": 1.0,
        "n_periods": len(rows),
        "batteries": [{"name": "home", "huge": "x" * 1000}],  # not kept
    }


CONFIG = {
    "friendly_name": "Nimbus Solver Config",
    "unresolved_required_keys": [],
    "solver_import_price_sensor": "sensor.test_buy",
    "solver_export_price_sensor": "sensor.test_sell",
    "solver_price_forecast_array_sensor": "sensor.test_array",
    "solver_battery_soc_sensor": "sensor.test_soc",  # not a price/forecast input
    "solver_p2p_block_1_rate_kw": 12.0,
}


class _Hass:
    def __init__(self, root: Path):
        self._root = root
        self.states_by_id: dict[str, SimpleNamespace] = {}
        self.config = SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts)))
        self.states = SimpleNamespace(get=self.states_by_id.get)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def set(self, entity_id: str, state, attributes: dict, updated: datetime):
        self.states_by_id[entity_id] = SimpleNamespace(
            state=state, attributes=attributes, last_updated=updated
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    store.reset_decision_inputs_cache()
    clock = {"now": T0}
    monkeypatch.setattr(store, "dt_util", SimpleNamespace(utcnow=lambda: clock["now"]))
    hass = _Hass(tmp_path)
    hass.set("sensor.nimbus_solver_config", "configured", dict(CONFIG), T0)
    for eid in ("sensor.test_buy", "sensor.test_sell", "sensor.test_array"):
        hass.set(eid, "0.3", {"forecast": [{"time": T0.isoformat(), "value": 0.3}]}, T0)
    yield hass, clock, tmp_path
    store.reset_decision_inputs_cache()


def _solve(hass, clock, at: datetime, battery_now: float):
    clock["now"] = at
    hass.set(
        "sensor.nimbus_solver_battery_forecast", battery_now, _plan(at, battery_now), at
    )
    return asyncio.run(store.async_capture(hass, "E"))


def _files(root: Path, kind: str) -> list[Path]:
    return sorted(
        (root / ".storage" / "nimbus_load_E_decision_inputs" / kind).glob("*.json.gz")
    )


def _read(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


# ---- pure ----------------------------------------------------------------


def test_dispatch_class_uses_the_automations_deadband():
    assert di.dispatch_class(0.06) == "discharge"
    assert di.dispatch_class(-0.06) == "charge"
    assert di.dispatch_class(0.05) == "self_consume"
    assert di.dispatch_class(-0.05) == "self_consume"
    assert di.dispatch_class(None) is None


def test_horizon_reason():
    assert di.horizon_reason(None, T0, None, "charge") == "first"
    t = T0 + timedelta(minutes=5)
    assert di.horizon_reason(T0, t, "charge", "charge") is None
    assert di.horizon_reason(T0, t, "charge", "self_consume") == "dispatch_class_change"
    assert (
        di.horizon_reason(T0, T0 + timedelta(minutes=30), "charge", "charge")
        == "interval"
    )


def test_head_is_two_hours_by_the_rows_own_lengths_and_keeps_only_row_fields():
    rows = _plan(T0, 3.0)["forecast"]
    head = di.columns(rows, di.HEAD_HOURS)
    assert set(head) == set(di.ROW_FIELDS)
    assert len(head["time"]) == 24  # 24 five-minute rows = 2 h
    full = di.columns(rows, None)
    assert len(full["time"]) == len(rows)


def test_a_trimmed_horizon_equals_the_head_that_would_have_been_filed():
    rec = di.build_record(
        _plan(T0, 3.0),
        captured_at=T0,
        kind=di.KIND_HORIZON,
        config_hash=None,
        sources=[],
        reason="first",
    )
    head = di.build_record(
        _plan(T0, 3.0), captured_at=T0, kind=di.KIND_HEAD, config_hash=None, sources=[]
    )
    assert di.as_head(rec)["rows"] == head["rows"]
    assert di.as_head(rec)["trimmed_to_head"] is True
    assert di.as_head(head) is head


def test_config_fingerprint_ignores_presentation_and_tracks_content():
    a = di.config_fingerprint(CONFIG)
    assert a == di.config_fingerprint({**CONFIG, "friendly_name": "renamed"})
    assert a != di.config_fingerprint({**CONFIG, "solver_p2p_block_1_rate_kw": 13.0})


def test_source_entities_are_the_price_and_forecast_inputs_only():
    # Sorted by config key, so the order is stable from capture to capture.
    assert di.source_entities(CONFIG) == [
        "sensor.test_sell",
        "sensor.test_buy",
        "sensor.test_array",
    ]


def test_expiry_counts_from_the_end_of_a_segment():
    now = T0
    keys = [
        di.segment_key(now - timedelta(hours=49, minutes=30)),
        di.segment_key(now - timedelta(hours=48, minutes=30)),
    ]
    # The first segment ended 49 h ago; the second ended exactly 48 h ago,
    # which is not yet past the 48 h retention.
    assert di.expired(keys, now, di.HEAD_KEEP) == [keys[0]]


def test_segment_key_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        di.segment_key(datetime(2026, 10, 9, 4))  # noqa: DTZ001 -- naive on purpose


# ---- store ---------------------------------------------------------------


def test_first_solve_files_a_horizon_then_heads_then_a_horizon_on_a_class_change(env):
    hass, clock, root = env
    assert _solve(hass, clock, T0, 3.0) == "horizon"
    assert _solve(hass, clock, T0 + timedelta(minutes=1), 3.1) == "head"
    assert (
        _solve(hass, clock, T0 + timedelta(minutes=2), 0.0) == "horizon"
    )  # discharge -> self-consume
    assert (
        _solve(hass, clock, T0 + timedelta(minutes=32), 0.0) == "horizon"
    )  # 30 min since the last
    asyncio.run(store.async_flush(hass, "E"))
    horizons = _read(_files(root, "horizon")[0])["records"]
    assert [r["reason"] for r in horizons] == [
        "first",
        "dispatch_class_change",
        "interval",
    ]
    heads = _read(_files(root, "head")[0])["records"]
    assert len(heads) == 1 and len(heads[0]["rows"]["time"]) == 24


def test_the_same_published_plan_is_not_filed_twice(env):
    hass, clock, _root = env
    assert _solve(hass, clock, T0, 3.0) == "horizon"
    clock["now"] = T0 + timedelta(minutes=1)
    assert asyncio.run(store.async_capture(hass, "E")) is None


def test_two_solves_in_one_period_are_both_filed(env):
    """`generated_at` is the plan's first period boundary, shared by every
    solve inside that period; identity is the entity's own write time."""
    hass, clock, _root = env
    plan = _plan(T0, 3.0)
    for second in (10, 40):
        clock["now"] = T0 + timedelta(seconds=second)
        hass.set("sensor.nimbus_solver_battery_forecast", 3.0, dict(plan), clock["now"])
        assert asyncio.run(store.async_capture(hass, "E")) is not None


def test_config_and_sources_travel_with_the_record(env):
    hass, clock, root = env
    _solve(hass, clock, T0, 3.0)
    asyncio.run(store.async_flush(hass, "E"))
    data = _read(_files(root, "horizon")[0])
    rec = data["records"][0]
    assert data["configs"][rec["config"]]["solver_p2p_block_1_rate_kw"] == 12.0
    assert "friendly_name" not in data["configs"][rec["config"]]
    assert {s["entity_id"] for s in rec["sources"]} == {
        "sensor.test_buy",
        "sensor.test_sell",
        "sensor.test_array",
    }
    assert rec["sources"][0]["forecast_rows"] == 1
    assert "batteries" not in rec  # large, and not an input


def test_a_restart_inside_the_hour_appends_rather_than_replacing(env):
    hass, clock, root = env
    _solve(hass, clock, T0, 3.0)
    asyncio.run(store.async_flush(hass, "E"))
    store.reset_decision_inputs_cache()  # what an unload/restart does
    _solve(hass, clock, T0 + timedelta(minutes=10), 3.0)  # first after restart: horizon
    asyncio.run(store.async_flush(hass, "E"))
    assert len(_read(_files(root, "horizon")[0])["records"]) == 2


def test_an_hour_rollover_writes_the_ended_hour_before_opening_the_next(env):
    """Regression: replacing the open segment must not drop its unwritten
    records (they are only flushed every FLUSH_EVERY)."""
    hass, clock, root = env
    _solve(hass, clock, T0 + timedelta(minutes=50), 3.0)  # horizon, flushed (first)
    _solve(hass, clock, T0 + timedelta(minutes=52), 3.0)  # head, NOT yet flushed
    _solve(hass, clock, T0 + timedelta(minutes=61), 3.0)  # next hour
    files = _files(root, "head")
    assert len(files) == 1 and len(_read(files[0])["records"]) == 1


def test_export_filters_by_window_and_kind(env):
    hass, clock, _root = env
    for m in range(0, 40, 1):
        _solve(hass, clock, T0 + timedelta(minutes=m), 3.0)
    start, end = T0, T0 + timedelta(minutes=40)
    both = asyncio.run(store.async_export(hass, "E", start, end, ["head", "horizon"]))
    assert both["count"] == 40
    heads = asyncio.run(store.async_export(hass, "E", start, end, ["head"]))
    assert heads["count"] == 40  # every solve, horizons trimmed to a head
    assert all(len(r["rows"]["time"]) == 24 for r in heads["records"])
    horizons = asyncio.run(store.async_export(hass, "E", start, end, ["horizon"]))
    assert [r["reason"] for r in horizons["records"]] == ["first", "interval"]
    assert len(both["configs"]) == 1
    part = asyncio.run(
        store.async_export(
            hass, "E", T0 + timedelta(minutes=10), T0 + timedelta(minutes=20), ["head"]
        )
    )
    assert part["count"] == 10


def test_export_refuses_a_window_over_24_hours_or_backwards(env):
    hass, _, _ = env
    with pytest.raises(ValueError):
        asyncio.run(
            store.async_export(hass, "E", T0, T0 + timedelta(hours=25), ["head"])
        )
    with pytest.raises(ValueError):
        asyncio.run(store.async_export(hass, "E", T0, T0, ["head"]))


def test_heads_go_after_48_hours_and_horizons_after_7_days(env):
    hass, clock, root = env
    _solve(hass, clock, T0, 3.0)  # horizon
    _solve(hass, clock, T0 + timedelta(minutes=1), 3.0)  # head
    asyncio.run(store.async_flush(hass, "E"))
    _solve(hass, clock, T0 + timedelta(hours=50), 3.0)  # a horizon (interval)
    asyncio.run(store.async_flush(hass, "E"))  # flush prunes
    assert [p.name for p in _files(root, "head")] == []  # the T0 head expired
    assert len(_files(root, "horizon")) == 2  # both horizons kept
    _solve(hass, clock, T0 + timedelta(days=8), -3.0)
    asyncio.run(store.async_flush(hass, "E"))
    # The T0 horizon is 8 days old and gone; the +50 h one (~5.9 days) stays.
    assert [p.name for p in _files(root, "horizon")] == [
        di.segment_key(T0 + timedelta(hours=50)) + ".json.gz",
        di.segment_key(T0 + timedelta(days=8)) + ".json.gz",
    ]


def test_capture_never_raises(env, monkeypatch):
    hass, _clock, _ = env
    hass.set(
        "sensor.nimbus_solver_battery_forecast", 1.0, {"forecast": "not a list"}, T0
    )
    assert asyncio.run(store.async_capture(hass, "E")) is None
    monkeypatch.setattr(
        store, "_state", lambda *_: (_ for _ in ()).throw(OSError("disk"))
    )
    hass.set("sensor.nimbus_solver_battery_forecast", 1.0, _plan(T0, 1.0), T0)
    assert asyncio.run(store.async_capture(hass, "E")) is None


def test_a_day_at_the_reference_shape_stays_small():
    """~1.1 solves a minute, a 202-row plan. Values are jittered per solve and
    per row so gzip cannot exploit repeats real plans do not have. Bound
    loosely; the real figure is measured on devhub and stated in the PR."""
    import random

    rng = random.Random(1657)

    def jittered(kind):
        plan = _plan(T0, 3.0)
        for row in plan["forecast"]:
            for k in (
                "import_price",
                "export_price",
                "load_kw",
                "solar_kw",
                "battery_kw",
                "soc_pct",
                "shadow_price",
            ):
                row[k] = round(rng.uniform(-1, 40), 4)
        return di.build_record(
            plan, captured_at=T0, kind=kind, config_hash="x" * 16, sources=[]
        )

    hour_of_heads = [jittered(di.KIND_HEAD) for _ in range(66)]
    hour_bytes = len(
        gzip.compress(json.dumps(hour_of_heads, separators=(",", ":")).encode())
    )
    one_horizon = len(
        gzip.compress(
            json.dumps(jittered(di.KIND_HORIZON), separators=(",", ":")).encode()
        )
    )
    heads_48h = hour_bytes * 48
    horizons_7d = one_horizon * 48 * 7 * 2  # every 30 min, doubled for class changes
    assert heads_48h < 25e6 and horizons_7d < 25e6, (heads_48h, horizons_7d)
