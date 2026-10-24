"""The day-ahead forecast snapshot store (#937): what tomorrow's forecast
regret is scored against (#1837).

Today's published plan is captured once a day, per config entry, survives a
restart through Home Assistant's Store, keeps 35 days, and is served from a
cache only when exactly one entry is loaded. Covers what lived in #937's
stage files and the snapshot-store files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

fss = importlib.import_module("custom_components.nimbus_load.forecast_snapshot_store")

BNE = ZoneInfo("Australia/Brisbane")


class Disk:
    """Home Assistant's Store, kept in a dict that outlives the cache."""

    files: ClassVar[dict[str, dict]] = {}
    saves: ClassVar[list[str]] = []
    fail_load = False

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        if Disk.fail_load:
            raise OSError("disk")
        return Disk.files.get(self.key)

    async def async_save(self, data):
        Disk.saves.append(self.key)
        Disk.files[self.key] = data


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(Disk, "files", {})
    monkeypatch.setattr(Disk, "saves", [])
    monkeypatch.setattr(Disk, "fail_load", False)
    monkeypatch.setattr(fss, "Store", Disk)
    clock = {"now": datetime(2026, 9, 20, 15, 0, tzinfo=BNE)}
    monkeypatch.setattr(
        fss,
        "dt_util",
        SimpleNamespace(
            now=lambda: clock["now"], parse_datetime=datetime.fromisoformat
        ),
    )
    fss.reset_snapshot_cache()
    yield clock
    fss.reset_snapshot_cache()


def _hass(rows):
    state = None if rows is None else SimpleNamespace(attributes={"forecast": rows})
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: state if eid == fss._SOURCE_ENTITY else None
        )
    )


def _rows(start, n=4):
    return [
        {
            "time": (start + timedelta(minutes=30 * i)).isoformat(),
            "solar_kw": float(i),
            "load_kw": 1.0 + i,
        }
        for i in range(n)
    ]


def _run(coro):
    return asyncio.run(coro)


def test_todays_plan_is_captured_once_and_survives_a_restart(store):
    hass = _hass(_rows(store["now"]))
    assert _run(fss.async_capture_todays_snapshot(hass, "E1")) is True
    assert _run(fss.async_capture_todays_snapshot(hass, "E1")) is False  # once a day
    assert len(Disk.saves) == 1
    key = fss.day_key_for(store["now"])
    snap = fss.get_cached_snapshot(key)
    assert snap is not None
    fss.reset_snapshot_cache()  # a restart: only the disk remains
    assert fss.get_cached_snapshot(key) is None
    assert _run(fss.async_load_snapshot(hass, "E1", key)) == snap
    assert _run(fss.async_load_snapshot(hass, "E1", "1999-01-01")) is None


def test_the_next_day_is_a_new_capture_and_old_days_are_pruned(store):
    hass = _hass(_rows(store["now"]))
    for _ in range(fss.KEEP_DAYS + 3):
        assert _run(fss.async_capture_todays_snapshot(hass, "E1")) is True
        store["now"] += timedelta(days=1)
    kept = next(iter(Disk.files.values()))
    assert len(kept) <= fss.KEEP_DAYS + 1
    first = fss.day_key_for(datetime(2026, 9, 20, 15, 0, tzinfo=BNE))
    assert first not in kept


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [{"time": "not a time", "solar_kw": 1, "load_kw": 1}],
        [{"time": "2026-09-20T15:00:00+10:00"}],
        ["x"],
    ],
    ids=["no_entity", "no_rows", "bad_time", "no_values", "not_a_row"],
)
def test_nothing_usable_published_is_no_snapshot(store, rows):
    assert _run(fss.async_capture_todays_snapshot(_hass(rows), "E1")) is False
    assert Disk.saves == []


def test_a_failing_store_never_raises(store):
    Disk.fail_load = True
    hass = _hass(_rows(store["now"]))
    assert _run(fss.async_capture_todays_snapshot(hass, "E1")) is False
    assert _run(fss.async_load_snapshot(hass, "E1", "x")) is None
    _run(fss.async_ensure_snapshots_loaded(hass, "E1"))


def test_the_cache_answers_only_when_one_entry_is_loaded(store):
    Disk.files[f"{fss.DOMAIN}_E1_forecast_snapshots"] = {"2026-09-19": {"a": 1}}
    Disk.files[f"{fss.DOMAIN}_E2_forecast_snapshots"] = {"2026-09-19": {"b": 2}}
    hass = _hass(None)
    _run(fss.async_ensure_snapshots_loaded(hass, "E1"))
    assert fss.get_cached_snapshot("2026-09-19") == {"a": 1}
    assert fss.get_cached_snapshot("2026-09-18") is None
    _run(fss.async_ensure_snapshots_loaded(hass, "E2"))
    assert fss.get_cached_snapshot("2026-09-19") is None  # whose would it be?
