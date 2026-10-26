"""The quality score's own archive (#1355): each day's score kept beyond the
sensor's rolling history, updated when its settlement lands, 120 days
deep (#1837).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

qh = importlib.import_module("custom_components.nimbus_load.quality_history_store")
NOW = datetime(2026, 9, 21, 0, 10, tzinfo=ZoneInfo("Australia/Brisbane"))


class Disk:
    def __init__(self, data=None, broken=False):
        self.data, self.broken, self.saves = data, broken, 0

    async def async_load(self):
        if self.broken:
            raise OSError("disk")
        return self.data

    async def async_save(self, data):
        self.saves += 1
        self.data = data


@pytest.fixture
def disk(monkeypatch):
    d = Disk()
    monkeypatch.setattr(qh, "Store", lambda hass, version, key: d)
    monkeypatch.setattr(qh, "dt_util", SimpleNamespace(now=lambda: NOW))
    qh.reset_quality_history_cache()
    yield d
    qh.reset_quality_history_cache()


def _hass(attrs):
    state = None if attrs is None else SimpleNamespace(attributes=attrs)
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: state if eid == qh._SOURCE_ENTITY else None
        )
    )


def _report(epr=0.9, provisional=True, **extra):
    return {
        "latest_date": "2026-09-20",
        "epr": epr,
        "epr_pct": epr * 100,
        "history": {"2026-09-20": {"p": 1} if provisional else {}},
        **extra,
    }


def test_a_days_score_is_archived_once_and_again_when_it_changes(disk):
    hass = _hass(_report())
    assert asyncio.run(qh.async_capture_quality_day(hass, "hub1")) is True
    assert asyncio.run(qh.async_capture_quality_day(hass, "hub1")) is False  # unchanged
    assert disk.saves == 1
    record = disk.data["2026-09-20"]
    assert record["provisional"] is True and record["captured_at"] == NOW.isoformat()
    settled = _hass(_report(epr=0.95, provisional=False))
    assert asyncio.run(qh.async_capture_quality_day(settled, "hub1")) is True
    assert (
        disk.data["2026-09-20"]["provisional"] is False
        and disk.data["2026-09-20"]["epr"] == 0.95
    )
    assert asyncio.run(qh.async_load_quality_history(hass, "hub1")) == disk.data


@pytest.mark.parametrize(
    "attrs",
    [
        None,
        {"epr": 0.9},
        {"latest_date": "2026-09-20", "epr": None},
        {"latest_date": "", "epr": 1},
    ],
)
def test_nothing_scored_is_nothing_archived(disk, attrs):
    assert asyncio.run(qh.async_capture_quality_day(_hass(attrs), "hub1")) is False
    assert disk.saves == 0


def test_the_archive_keeps_the_newest_days(disk):
    # test_the_archive_keeps_the_newest_days
    stored = {f"2026-01-{d:02d}": {} for d in range(1, 11)}
    assert list(qh.prune(stored, keep_days=3)) == [
        "2026-01-08",
        "2026-01-09",
        "2026-01-10",
    ]
    assert qh.prune(stored, keep_days=20) is stored

    # test_a_failing_store_never_raises
    disk.broken = True
    assert asyncio.run(qh.async_capture_quality_day(_hass(_report()), "hub1")) is False
    assert asyncio.run(qh.async_load_quality_history(_hass(None), "hub1")) == {}

    # test_the_captured_time_is_not_part_of_what_changed
    _day, a = qh.build_quality_capture(_report(), captured_at="t1")
    _day, b = qh.build_quality_capture(_report(), captured_at="t2")
    assert qh.fingerprint(a) == qh.fingerprint(b)
    _day, c = qh.build_quality_capture(
        {**_report(), "hourly_regret": [1, 2]}, captured_at="t1"
    )
    assert ("hourly_regret" in c) is ("hourly_regret" in qh._HOURLY_KEYS)
