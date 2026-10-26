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


def _history(entity_id, start, end, *, unit="W", fn=None):
    """Recorder rows every 15 minutes, W unless told otherwise."""
    rows = []
    t = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
    fn = fn or (lambda t: _house_kw(t))  # looked up per call: a test may change it
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


def test_a_retrain_then_refresh_publishes_the_learned_forecast(
    tmp_path, recorder, site, monkeypatch, caplog
):
    # test_a_retrain_then_refresh_publishes_the_learned_forecast
    tmp_0 = tmp_path / "test_a_retrain_then_refresh_publishes_the_learned_forecast"
    tmp_0.mkdir()
    hass = FakeHass(tmp_0)
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

    # test_a_refresh_while_home_assistant_stops_keeps_the_last_data
    tmp_1 = tmp_path / "test_a_refresh_while_home_assistant_stops_keeps_the_last_data"
    tmp_1.mkdir()
    hass = FakeHass(tmp_1)
    coord = _coordinator(hass)
    coord.data = {"sentinel": True}
    hass.is_stopping = True
    assert asyncio.run(coord._async_update_data()) == {"sentinel": True}

    # test_every_site_input_is_read_in_kw_and_trained_on
    tmp_2 = tmp_path / "test_every_site_input_is_read_in_kw_and_trained_on"
    tmp_2.mkdir()
    hass = _site_hass(tmp_2)
    coord = _site_coordinator(hass)
    asyncio.run(coord._async_retrain())
    assert coord._last_retrain_error is None
    read = {r[0] for r in hass.history_reads}
    assert read >= {
        LOAD,
        "sensor.outside_temp",
        "sensor.outside_humidity",
        "switch.curtail",
        "sensor.battery_power",
        "sensor.grid_power",
        "sensor.solar_power",
    }
    x = coord._trained.x_train
    for feature in ("temp_c", "curtailment", "battery_kw", "solar_kw"):
        assert x[:, cm.FEATURE_NAMES.index(feature)].std() > 0, feature
    # Live readings are in kW whatever the sensor's unit.
    assert coord._current_measured_power("sensor.battery_power") == pytest.approx(0.5)
    assert coord._current_measured_power("sensor.missing") == 0.0
    assert coord._current_humidity() == 60.0
    assert coord.data["forecast"]

    # test_an_energy_sensor_is_never_a_power_feature_and_the_load_never_its_own
    tmp_3 = (
        tmp_path
        / "test_an_energy_sensor_is_never_a_power_feature_and_the_load_never_its_own"
    )
    tmp_3.mkdir()
    hass = _site_hass(tmp_3)
    coord = _site_coordinator(
        hass,
        **{
            const.CONF_BATTERY_SENSOR: "sensor.battery_energy",
            const.CONF_HUMIDITY_SENSOR: LOAD,
        },
    )
    assert coord._battery_sensor is None  # kWh: energy, not power (#1562)
    assert coord._humidity_sensor is None  # the load itself (#1540)
    assert coord._grid_sensor == "sensor.grid_power"

    # test_a_weather_entity_is_read_through_its_forecast_service
    tmp_4 = tmp_path / "test_a_weather_entity_is_read_through_its_forecast_service"
    tmp_4.mkdir()
    hass = _site_hass(tmp_4)
    coord = _site_coordinator(
        hass, **{const.CONF_TEMPERATURE_FORECAST_SENSOR: "weather.home"}
    )
    got = asyncio.run(coord._async_fetch_temperature_forecast())
    assert len(got) == 100 and got[0][1] == 20.0
    assert hass.service_calls == [
        ("weather", "get_forecasts", {"entity_id": "weather.home"})
    ]
    hass._states["weather.home"] = _State(state="unavailable", attributes={})
    # Unavailable after a restart: no call, and the last good forecast is kept.
    again = asyncio.run(coord._async_fetch_temperature_forecast())
    assert len(hass.service_calls) == 1 and again and again[0][0] >= NOW

    # test_long_term_statistics_train_the_older_days
    tmp_5 = tmp_path / "test_long_term_statistics_train_the_older_days"
    tmp_5.mkdir()
    for source in [const.TRAINING_SOURCE_LTS, const.TRAINING_SOURCE_HYBRID]:
        hass = _site_hass(tmp_5)
        coord = _site_coordinator(
            hass,
            **{const.CONF_TRAINING_SOURCE: source, const.CONF_HYBRID_RECENT_DAYS: 5},
        )
        asyncio.run(coord._async_retrain())
        assert coord._last_retrain_error is None and coord._trained is not None
        lts = [r for r in hass.lts_reads if r[0] == LOAD]
        assert lts, "the load was read from long-term statistics"
        start = NOW - timedelta(days=21)
        if source == const.TRAINING_SOURCE_HYBRID:
            # Statistics for the older days, the recorder for the last five.
            assert lts[0][2] == NOW - timedelta(days=5)
            assert any(
                r[0] == LOAD and r[1] == NOW - timedelta(days=5)
                for r in hass.history_reads
            )
            assert coord._change_only_from(start, NOW) == NOW - timedelta(days=5)
        else:
            assert coord._change_only_from(start, NOW) is None

    # --- what is kept across restarts ------------------------------------------------
    # test_setup_loads_what_was_kept_and_schedules_the_nightly_retrain
    tmp_6 = (
        tmp_path / "test_setup_loads_what_was_kept_and_schedules_the_nightly_retrain"
    )
    tmp_6.mkdir()
    with monkeypatch.context() as mp:
        hass = _site_hass(tmp_6)
        first = _coordinator(hass)
        asyncio.run(first._async_retrain())
        first._residuals = [0.1, 0.2]
        first._save_residuals_to_disk()
        tracked, created, unloads = [], [], []

        def track(hass_, cb, **kw):
            tracked.append(kw)
            return lambda: tracked.append("off")

        mp.setattr(cm, "async_track_time_change", track)
        hass.async_create_task = lambda coro: created.append(coro) or coro.close()
        second = _coordinator(hass)
        second.entry.async_on_unload = unloads.append
        asyncio.run(second.async_setup())
        assert second._trained is not None and second._residuals == [0.1, 0.2]
        assert tracked == [
            {
                "hour": second._retrain_hour,
                "minute": second._retrain_minute,
                "second": 0,
            }
        ]
        assert created == []  # a current model needs no retrain at startup
        unloads[0]()
        assert tracked[-1] == "off"

    # test_a_damaged_or_foreign_model_file_is_discarded_and_retrained
    tmp_7 = tmp_path / "test_a_damaged_or_foreign_model_file_is_discarded_and_retrained"
    tmp_7.mkdir()
    with monkeypatch.context() as mp:
        for damage in ["garbage", "features", "newer"]:
            with mp.context() as mp:
                hass = _site_hass(tmp_7)
                coord = _coordinator(hass)
                asyncio.run(coord._async_retrain())
                trained = coord._trained
                if damage == "garbage":
                    coord._model_path.write_bytes(b"not a pickle")
                elif damage == "features":
                    trained.x_mean = trained.x_mean[:-1]
                    coord._save_model_to_disk(trained)
                else:
                    trained.schema_version = cm.TRAINED_MODEL_SCHEMA_VERSION + 1
                    coord._save_model_to_disk(trained)
                coord._residual_path.write_text('["x"]', encoding="utf-8")
                created = []
                mp.setattr(cm, "async_track_time_change", lambda *a, **k: lambda: None)
                hass.async_create_task = lambda coro: (
                    created.append(coro) or coro.close()  # noqa: B023
                )
                again = _coordinator(hass)
                again.entry.async_on_unload = lambda cb: None
                asyncio.run(again.async_setup())
                assert again._trained is None and again._residuals == []
                assert len(created) == 1  # retrain now, not at tonight's hour

    # test_each_refresh_scores_the_last_prediction_and_flags_drift_once
    caplog.clear()
    tmp_8 = (
        tmp_path / "test_each_refresh_scores_the_last_prediction_and_flags_drift_once"
    )
    tmp_8.mkdir()
    with monkeypatch.context() as mp:
        hass = _site_hass(tmp_8)
        coord = _coordinator(hass)
        asyncio.run(coord._async_retrain())
        coord._residuals = [0.1] * 20
        clock = {"now": NOW}
        mp.setattr(cm.dt_util, "utcnow", lambda: clock["now"])
        # The house runs well over its forecast from here on.
        mp.setattr(sys.modules[__name__], "_house_kw", lambda t: 7.5)
        with caplog.at_level("WARNING", logger=cm._LOGGER.name):
            for _ in range(12):
                clock["now"] += timedelta(minutes=15)
                asyncio.run(coord._async_update_data())
        # The retrain's own refresh left a prediction, so every refresh scores one.
        assert len(coord._residuals) == 20 + 12
        assert coord._residual_drift_status.watching is True
        assert (
            sum("residual drift detected" in r.getMessage() for r in caplog.records)
            == 1
        )
        assert coord._residual_path.exists()

    # test_a_loads_running_hold_reaches_the_predict_call: #1703: the subentry's threshold and hold length are what predict() runs with.
    tmp_9 = tmp_path / "test_a_loads_running_hold_reaches_the_predict_call"
    tmp_9.mkdir()
    with monkeypatch.context() as mp:
        hass = FakeHass(tmp_9)
        coord = _coordinator(
            hass,
            **{const.CONF_RUNNING_THRESHOLD_KW: 1.0, const.CONF_RUN_HOLD_HOURS: 2.0},
        )
        asyncio.run(coord._async_retrain())
        seen = []
        real = cm.predict

        def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return real(*args, **kwargs)

        mp.setattr(cm, "predict", spy)
        asyncio.run(coord._async_update_data())
        assert seen
        for args, kwargs in seen:
            threshold = kwargs.get(
                "running_threshold_kw", args[15] if len(args) > 15 else None
            )
            hold = kwargs.get("run_hold_hours", args[16] if len(args) > 16 else None)
            assert (threshold, hold) == (1.0, 2.0)


# --- a whole site's inputs ------------------------------------------------------

SITE = {
    "sensor.outside_temp": (
        lambda t: 18.0 + 6.0 * ((t.astimezone(BNE).hour - 3) % 24) / 24,
        "°C",
    ),
    "sensor.outside_humidity": (lambda t: 60.0, "%"),
    "sensor.battery_power": (
        lambda t: -2000.0 if 9 <= t.astimezone(BNE).hour < 15 else 500.0,
        "W",
    ),
    "sensor.grid_power": (lambda t: 300.0, "W"),
    "sensor.solar_power": (
        lambda t: 4000.0 if 8 <= t.astimezone(BNE).hour < 16 else 0.0,
        "W",
    ),
    "sensor.battery_energy": (lambda t: 12.0, "kWh"),
}


def _site_rows(entity, start, end):
    fn, unit = SITE[entity]
    if unit == "W":
        return _history(entity, start, end, unit="W", fn=lambda t: fn(t) / 1000.0)
    rows = _history(entity, start, end, unit="kW", fn=fn)
    for r in rows:
        r.attributes = {"unit_of_measurement": unit}
    return rows


@pytest.fixture
def site(monkeypatch, recorder):
    def significant(hass, start, end, ids, **kw):
        entity = ids[0]
        hass.history_reads.append((entity, start, end))
        if entity == "switch.curtail":
            rows = [
                _State(
                    state="on" if i % 2 else "off",
                    attributes={},
                    last_changed=start + timedelta(hours=i),
                )
                for i in range(int((end - start).total_seconds() // 3600))
            ]
            return {entity: rows}
        if entity in SITE:
            return {entity: _site_rows(entity, start, end)}
        return {entity: _history(entity, start, end, unit=hass.unit)}

    def statistics(hass, start, end, ids, period, units, types_):
        (entity,) = ids
        hass.lts_reads.append((entity, start, end))
        rows, t = [], start
        while t < end:
            if entity == LOAD:
                mean = _house_kw(t) * (1000.0 if hass.unit == "W" else 1.0)
            else:
                mean = 20.0
            rows.append({"start": t.timestamp(), "mean": mean})
            t += timedelta(hours=1)
        rows.append({"start": t.isoformat(), "mean": None})
        rows.append({"start": t.isoformat(), "mean": 5e9})
        return {entity: rows}

    monkeypatch.setattr(cm, "get_significant_states", significant)
    monkeypatch.setattr(cm, "statistics_during_period", statistics)


def _site_hass(tmp, **kw):
    hass = FakeHass(tmp, **kw)
    hass.lts_reads = []
    for entity, (fn, unit) in SITE.items():
        hass._states[entity] = _State(
            state=str(fn(NOW)), attributes={"unit_of_measurement": unit}
        )
    hass._states["switch.curtail"] = _State(state="off", attributes={})
    forecast = [
        {
            "datetime": (NOW + timedelta(hours=h)).isoformat(),
            "temperature": 20.0 + h % 5,
        }
        for h in range(100)
    ]
    hass._states["sensor.temp_forecast"] = _State(
        state="20", attributes={"forecast": [*forecast, {"datetime": "x"}]}
    )
    hass._states["weather.home"] = _State(state="sunny", attributes={})
    hass.service_calls = []

    async def services(domain, service, data, target, blocking, return_response):
        hass.service_calls.append((domain, service, target))
        return {target["entity_id"]: {"forecast": forecast}}

    hass.services = SimpleNamespace(async_call=services)
    return hass


def _site_coordinator(hass, **options):
    coord = _coordinator(hass)
    coord.entry.options.update(
        {
            const.CONF_TEMPERATURE_SENSOR: "sensor.outside_temp",
            const.CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.temp_forecast",
            const.CONF_HUMIDITY_SENSOR: "sensor.outside_humidity",
            const.CONF_CURTAILMENT_SENSOR: "switch.curtail",
            const.CONF_BATTERY_SENSOR: "sensor.battery_power",
            const.CONF_GRID_SENSOR: "sensor.grid_power",
            const.CONF_SOLAR_SENSOR: "sensor.solar_power",
            **options,
        }
    )
    return coord
