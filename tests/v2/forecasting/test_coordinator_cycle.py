"""One load's forecaster inside Home Assistant: retrain from the recorder,
publish a forecast, keep the model across a restart (#1837).

A fake recorder answers from three weeks of synthetic 15-minute history for
a house with an evening peak; everything between it and the published
forecast is the coordinator's own code. Covers what lived in #1195, #1217,
#1540, #1556, #1562, #1566, #1575 and the coordinator files.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# The repository root, for the package import; tests/ is already on the
# path (pyproject), and must not shadow the package's own module names.
sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

cm = importlib.import_module("custom_components.nimbus_load.coordinator")
const = importlib.import_module("custom_components.nimbus_load.const")

BNE = ZoneInfo("Australia/Brisbane")
NOW = datetime(2026, 9, 20, 6, 7, tzinfo=UTC)  # 16:07 Brisbane
LOAD = "sensor.house_power"


def _house_kw(t: datetime) -> float:
    local = t.astimezone(BNE)
    return 2.5 if 17 <= local.hour < 21 else 0.4


class _State(SimpleNamespace):
    pass


def _history(entity_id, start, end, *, unit="W", fn=_house_kw):
    """Recorder rows every 15 minutes, W unless told otherwise."""
    rows = []
    t = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
    scale = 1000.0 if unit == "W" else 1.0
    while t < end:
        if t >= start:
            rows.append(
                _State(
                    entity_id=entity_id,
                    state=str(round(fn(t) * scale, 3)),
                    attributes={"unit_of_measurement": unit},
                    last_changed=t,
                    last_updated=t,
                )
            )
        t += timedelta(minutes=15)
    return rows


class FakeHass:
    def __init__(self, tmp: Path, *, unit="W"):
        self.is_stopping = False
        self.unit = unit
        self.config = SimpleNamespace(
            path=lambda *p: str(tmp.joinpath(*p)), time_zone="Australia/Brisbane"
        )
        (tmp / ".storage").mkdir(exist_ok=True)
        self._states = {
            LOAD: _State(
                state=str(_house_kw(NOW) * 1000),
                attributes={"unit_of_measurement": unit},
            )
        }
        self.states = SimpleNamespace(get=self._states.get)
        self.services = SimpleNamespace(async_call=self._no_service)
        self.history_reads: list[tuple] = []

    async def _no_service(self, *a, **k):
        raise RuntimeError("no weather service in this test")

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


@pytest.fixture
def recorder(monkeypatch):
    def significant(hass, start, end, ids, **kw):
        hass.history_reads.append((ids[0], start, end))
        return {ids[0]: _history(ids[0], start, end, unit=hass.unit)}

    class Instance:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    monkeypatch.setattr(cm, "get_significant_states", significant)
    monkeypatch.setattr(cm, "get_instance", lambda hass: Instance())
    monkeypatch.setattr(
        cm,
        "dt_util",
        SimpleNamespace(
            utcnow=lambda: NOW,
            as_local=lambda t: t.astimezone(BNE),
            as_utc=lambda t: t.astimezone(UTC),
            parse_datetime=datetime.fromisoformat,
            utc_from_timestamp=lambda s: datetime.fromtimestamp(s, UTC),
        ),
    )


def _coordinator(hass, **data):
    entry = SimpleNamespace(
        options={const.CONF_FORECAST_HORIZON_HOURS: 48}, entry_id="hub"
    )
    subentry = SimpleNamespace(
        subentry_id="house",
        subentry_type=const.SUBENTRY_TYPE_LOAD,
        title="House",
        data={const.CONF_LOAD_SENSOR: LOAD, const.CONF_TRAIN_DAYS: 21, **data},
    )
    coord = cm.NimbusCoordinator(hass, entry, subentry)

    async def refresh():
        coord.data = await coord._async_update_data()

    coord.async_request_refresh = refresh
    return coord


def test_a_retrain_then_refresh_publishes_the_learned_forecast(tmp_path, recorder):
    hass = FakeHass(tmp_path)
    coord = _coordinator(hass)
    before = asyncio.run(coord._async_update_data())
    assert (
        before["forecast"] == []
        and before["state"] is None
        and before["training_points"] == 0
    )

    asyncio.run(coord._async_retrain())
    assert coord._last_retrain_error is None
    data = coord.data
    assert data["training_points"] > 1000 and data["model_type"] in {
        "gbrt",
        "knn",
        "naive",
    }
    forecast = data["forecast"]
    assert len(forecast) >= 48 * 4
    times = [datetime.fromisoformat(p["time"]) for p in forecast]
    # Now first, then on the clock's quarter hours (#1575, 13 Oct 2026).
    assert times[0] == NOW.astimezone(BNE)
    assert times[1] == datetime(2026, 9, 20, 16, 15, tzinfo=BNE)
    assert all(b - a == timedelta(minutes=15) for a, b in itertools.pairwise(times[1:]))
    # A configured 48 h is raised to cover the Solver's plan (#1566).
    assert coord._horizon_hours == max(48, cm.MIN_FORECAST_HORIZON_HOURS) > 48
    assert NOW + timedelta(hours=coord._horizon_hours - 0.25) < times[-1]
    assert times[-1] <= NOW + timedelta(hours=coord._horizon_hours)
    for p in forecast:
        assert 0.0 <= p["lower"] <= p["value"] + 1e-9 <= p["upper"] + 2e-9
    peak = [
        p["value"] for p, t in zip(forecast, times, strict=True) if 17 <= t.hour < 21
    ]
    quiet = [
        p["value"] for p, t in zip(forecast, times, strict=True) if 2 <= t.hour < 12
    ]
    # Recorded in W, forecast in kW.
    assert sum(peak) / len(peak) > sum(quiet) / len(quiet) + 1.5
    assert max(p["value"] for p in forecast) < 5.0
    assert data["forecast_origin"] == const.FORECAST_ORIGIN_LEARNED


def test_the_model_survives_a_restart(tmp_path, recorder):
    hass = FakeHass(tmp_path)
    first = _coordinator(hass)
    asyncio.run(first._async_retrain())
    second = _coordinator(hass)
    loaded = second._load_model_from_disk()
    assert (
        loaded is not None and loaded.training_points == first._trained.training_points
    )
    second._trained = loaded
    again = asyncio.run(second._async_update_data())
    assert [p["value"] for p in again["forecast"]] == [
        p["value"] for p in first.data["forecast"]
    ]


def test_a_kw_sensor_trains_to_the_same_forecast_as_a_w_sensor(tmp_path, recorder):
    (tmp_path / "w").mkdir()
    (tmp_path / "kw").mkdir()
    w = _coordinator(FakeHass(tmp_path / "w"))
    asyncio.run(w._async_retrain())
    kw = _coordinator(FakeHass(tmp_path / "kw", unit="kW"))
    asyncio.run(kw._async_retrain())
    assert [p["value"] for p in kw.data["forecast"]] == pytest.approx(
        [p["value"] for p in w.data["forecast"]], abs=1e-3
    )


def test_a_scheduled_load_publishes_its_rule_without_any_model(tmp_path, recorder):
    coord = _coordinator(
        FakeHass(tmp_path),
        **{
            const.CONF_EXPECTED_LOAD_KW: 1.3,
            const.CONF_SCHEDULE_START_HOUR: "08:00:00",
            const.CONF_SCHEDULE_END_HOUR: "15:00:00",
        },
    )
    data = asyncio.run(coord._async_update_data())
    assert data["forecast_origin"] == const.FORECAST_ORIGIN_RULE
    for p in data["forecast"]:
        hour = datetime.fromisoformat(p["time"]).hour
        assert (
            p["value"] == (1.3 if 8 <= hour < 15 else 0.0) == p["lower"] == p["upper"]
        )


def test_a_refresh_while_home_assistant_stops_keeps_the_last_data(tmp_path, recorder):
    hass = FakeHass(tmp_path)
    coord = _coordinator(hass)
    coord.data = {"sentinel": True}
    hass.is_stopping = True
    assert asyncio.run(coord._async_update_data()) == {"sentinel": True}
