"""The inputs each decision was made on, captured so the solve can be replayed
(#1657) (#1837).
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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


def test_source_entities_are_the_price_and_forecast_inputs_only(env, monkeypatch):
    # test_source_entities_are_the_price_and_forecast_inputs_only
    # Sorted by config key, so the order is stable from capture to capture.
    assert di.source_entities(CONFIG) == [
        "sensor.test_sell",
        "sensor.test_buy",
        "sensor.test_array",
    ]

    # test_a_restart_inside_the_hour_appends_rather_than_replacing
    hass, clock, root = env
    _solve(hass, clock, T0, 3.0)
    asyncio.run(store.async_flush(hass, "E"))
    store.reset_decision_inputs_cache()  # what an unload/restart does
    _solve(hass, clock, T0 + timedelta(minutes=10), 3.0)  # first after restart: horizon
    asyncio.run(store.async_flush(hass, "E"))
    assert len(_read(_files(root, "horizon")[0])["records"]) == 2

    # test_export_filters_by_window_and_kind
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

    # test_heads_go_after_48_hours_and_horizons_after_7_days
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

    # test_capture_never_raises
    with monkeypatch.context() as mp:
        hass, _clock, _ = env
        hass.set(
            "sensor.nimbus_solver_battery_forecast", 1.0, {"forecast": "not a list"}, T0
        )
        assert asyncio.run(store.async_capture(hass, "E")) is None
        mp.setattr(store, "_state", lambda *_: (_ for _ in ()).throw(OSError("disk")))
        hass.set("sensor.nimbus_solver_battery_forecast", 1.0, _plan(T0, 1.0), T0)
        assert asyncio.run(store.async_capture(hass, "E")) is None


# ---- store ---------------------------------------------------------------
