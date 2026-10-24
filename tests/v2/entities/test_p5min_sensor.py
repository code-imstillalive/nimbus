"""`sensor.nimbus_p5min_forecast`: AEMO's 5-minute pre-dispatch, fetched
from NEMweb once per run, published for the household's region (#1837).

Driven end to end through a fake NEMweb that serves AEMO's own public files
(tests/fixtures: market data, not household data): the listing, the zipped
run, and the DispatchIS file the #1778 check compares against. Covers what
lived in #1653, #1660, #1668, #1778 and the P5MIN sensor files.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

ps = importlib.import_module("custom_components.nimbus_load.sensor_p5min")
p5min = importlib.import_module("custom_components.nimbus_load.p5min")
dispatch_rhs = importlib.import_module("custom_components.nimbus_load.dispatch_rhs")
const = importlib.import_module("custom_components.nimbus_load.const")

FIX = Path(__file__).resolve().parents[2] / "fixtures"
RUNS = sorted((FIX / "dispatch_rhs").glob("PUBLIC_P5MIN_*.CSV"))
DISPATCH = sorted((FIX / "dispatch_rhs").glob("PUBLIC_DISPATCHIS_*.CSV"))
REGION = "QLD1"


def _zip(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(path.name, path.read_text())
    return buf.getvalue()


class Response:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self.body

    async def read(self):
        return self.body


class NEMweb:
    """Serves one P5MIN run and one DispatchIS interval at a time."""

    def __init__(self):
        self.run = RUNS[0]
        self.dispatch = DISPATCH[0]
        self.fail = None
        self.dispatch_fail = None
        self.gets: list[str] = []

    def _zip_name(self, path):
        return path.name[:-4] + ".zip"

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url == p5min.BASE_URL:
            if self.fail:
                return Response(self.fail)
            return Response(f'<a href="/x/{self._zip_name(self.run)}">')
        if url == p5min.BASE_URL + self._zip_name(self.run):
            return Response(_zip(self.run))
        if url == dispatch_rhs.BASE_URL:
            if self.dispatch_fail:
                return Response(self.dispatch_fail)
            return Response(f'<a href="/x/{self._zip_name(self.dispatch)}">')
        if url == dispatch_rhs.BASE_URL + self._zip_name(self.dispatch):
            return Response(_zip(self.dispatch))
        return Response(RuntimeError(f"404 {url}"))


@pytest.fixture
def nemweb(monkeypatch):
    web = NEMweb()
    client = (
        importlib.import_module("homeassistant.helpers.aiohttp_client")
        if "homeassistant.helpers.aiohttp_client" in sys.modules
        else None
    )
    if client is None:
        import types

        client = types.ModuleType("homeassistant.helpers.aiohttp_client")
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", client)
    monkeypatch.setattr(
        client, "async_get_clientsession", lambda hass: web, raising=False
    )
    return web


def _sensor(states=None):
    entry = SimpleNamespace(
        entry_id="hub1",
        data={},
        options={
            const.CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: "sensor.qld_price"
        },
    )
    s = ps.NimbusP5MinForecastSensor(entry, REGION, "0.94.485")
    states = states or {}

    async def executor(fn, *a):
        return fn(*a)

    s.hass = SimpleNamespace(
        states=SimpleNamespace(get=states.get), async_add_executor_job=executor
    )
    s.writes = 0

    def write():
        s.writes += 1

    s.async_write_ha_state = write
    return s


def test_a_run_is_fetched_and_its_region_published(nemweb):
    s = _sensor()
    assert not s.available and s.native_value is None
    asyncio.run(s._async_tick())
    expected = p5min.parse_run(RUNS[0].read_text(), REGION)
    assert s.available
    assert s.native_value == expected["forecast"][0]["value"]
    attrs = s.extra_state_attributes
    assert attrs["region"] == REGION and attrs["source_file"] == nemweb._zip_name(
        RUNS[0]
    )
    assert (
        attrs["run_datetime"] == expected["run_datetime"]
        and attrs["last_error"] is None
    )
    assert attrs["forecast"] == expected["forecast"]
    assert attrs["max_price"] == p5min.peak(expected["forecast"])["value"]
    assert attrs["dispatch_rhs_source_file"] == nemweb._zip_name(DISPATCH[0])
    assert attrs["readiness_signal_mode"] == "shadow"
    assert {"forecast", "previous_forecast"} <= s._unrecorded_attributes


def test_nothing_is_fetched_again_until_the_next_run_can_exist(nemweb, monkeypatch):
    s = _sensor()
    asyncio.run(s._async_tick())
    run = datetime.fromisoformat(s._run["run_datetime"])
    assert not s.due(run + timedelta(minutes=1)) and s.due(run + ps._NEXT_RUN_DUE)
    fetched = len(nemweb.gets)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return run + timedelta(seconds=30)

    monkeypatch.setattr(ps, "datetime", Clock)
    asyncio.run(s._async_tick())
    assert len(nemweb.gets) == fetched


def test_the_same_file_listed_again_is_not_downloaded_again(nemweb):
    s = _sensor()
    asyncio.run(s._async_tick())
    s._run["run_datetime"] = "2000-01-01T00:00:00+10:00"  # due again
    downloads = sum(u.endswith(".zip") for u in nemweb.gets)
    asyncio.run(s._async_tick())
    assert sum(u.endswith(".zip") for u in nemweb.gets) == downloads


def test_a_new_run_keeps_the_previous_one_for_comparison(nemweb):
    s = _sensor()
    s.due = lambda now: True
    asyncio.run(s._async_tick())
    first = s._run["run_datetime"]
    nemweb.run = RUNS[1]
    asyncio.run(s._async_tick())
    assert s._run["run_datetime"] != first
    assert s.extra_state_attributes["previous_run_datetime"] == first
    assert s.extra_state_attributes["previous_forecast"]


def test_a_network_fault_is_recorded_and_the_last_run_kept(nemweb):
    s = _sensor()
    asyncio.run(s._async_tick())
    good = s.native_value
    s._run["run_datetime"] = "2000-01-01T00:00:00+10:00"
    nemweb.fail = TimeoutError("nemweb slow")
    asyncio.run(s._async_tick())
    assert s.native_value == good
    assert s.extra_state_attributes["last_error"] == "TimeoutError: nemweb slow"


def test_a_dispatch_fault_never_blocks_the_run(nemweb):
    nemweb.dispatch_fail = RuntimeError("down")
    s = _sensor()
    asyncio.run(s._async_tick())
    assert s.available
    assert s.extra_state_attributes["dispatch_rhs_last_error"] == "RuntimeError: down"


def test_a_file_without_the_region_keeps_the_last_good_run(nemweb):
    s = _sensor()
    asyncio.run(s._async_tick())
    good = s._run
    s.apply("PUBLIC_P5MIN_202610161055_20261016105104.zip", None)
    assert s._run is good
    assert (
        s.extra_state_attributes["last_error"]
        == f"no {REGION} rows in PUBLIC_P5MIN_202610161055_20261016105104.zip"
    )


def test_the_spot_price_feeds_the_false_spike_guard_each_minute(nemweb):
    states = {
        "sensor.qld_price": SimpleNamespace(
            state="0.12",
            attributes={"unit_of_measurement": "$/kWh"},
            last_updated=datetime(2026, 10, 16, 0, 50, tzinfo=UTC),
        )
    }
    s = _sensor(states)
    asyncio.run(s._async_tick())
    s.check_dispatch(datetime(2026, 10, 16, 0, 51, tzinfo=UTC))
    # The live spot price, in $/kWh, is recorded against its interval.
    (compared,) = s._guard.compared
    assert compared["dispatch"] == 0.12 and s._guard.last_dispatch == 0.12
    assert s.extra_state_attributes["forecast_at_dispatch"] == [compared]
    states["sensor.qld_price"].state = "n/a"  # unreadable: nothing recorded
    s.check_dispatch(datetime(2026, 10, 16, 0, 52, tzinfo=UTC))
    assert len(s._guard.compared) == 1
    s._dispatch_sensor = None
    s.check_dispatch(datetime(2026, 10, 16, 0, 53, tzinfo=UTC))
    assert len(s._guard.compared) == 1


def test_the_threshold_number_is_read_live():
    s = _sensor(
        {
            f"number.nimbus_{const.CONF_SOLVER_P5MIN_FALSE_SPIKE_THRESHOLD_MWH}": SimpleNamespace(
                state="900"
            )
        }
    )
    assert s._threshold_mwh() == 900.0
    s.hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state="x"))
    assert s._threshold_mwh() == s._guard.threshold_mwh


# --- which region ------------------------------------------------------------------------


def _entry(**options):
    return SimpleNamespace(data={}, options=options)


def _hass_located(region):
    return SimpleNamespace(
        states=SimpleNamespace(async_all=lambda domain: []), region=region
    )


@pytest.mark.parametrize(
    ("options", "geocoded", "region"),
    [
        (
            {
                const.CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.aemo_nem_qld1_forecast"
            },
            "VIC1",
            "QLD1",
        ),
        ({}, "VIC1", "VIC1"),
        ({}, None, None),
        ({}, "WA1", None),
    ],
)
def test_the_region_is_the_configured_sensors_else_the_geocoded_location(
    monkeypatch, options, geocoded, region
):
    monkeypatch.setattr(
        ps.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        lambda states: (geocoded, "410"),
    )
    assert ps.resolve_region(_hass_located(geocoded), _entry(**options)) == region


def test_sensors_naming_two_regions_are_a_contradiction_not_a_guess(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        ps.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        lambda states: ("QLD1", "410"),
    )
    entry = _entry(
        **{
            const.CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.aemo_nem_qld1_forecast",
            const.CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: "sensor.aemo_nem_nsw1_price",
        }
    )
    with caplog.at_level(logging.WARNING, logger=ps._LOGGER.name):
        assert ps.resolve_region(_hass_located("QLD1"), entry) is None
    assert "NSW1, QLD1" in caplog.text


def test_a_zipped_run_is_read_as_text():
    assert ps._unzip_text(_zip(RUNS[0])) == RUNS[0].read_text()


def test_setup_schedules_the_minute_poll_and_a_first_fetch(monkeypatch):
    s = _sensor()
    polls, tasks = [], []
    monkeypatch.setattr(
        ps,
        "async_track_time_interval",
        lambda hass, cb, every: polls.append(every) or (lambda: None),
    )
    s.hass.async_create_background_task = lambda coro, name: (
        tasks.append(name) or coro.close()
    )
    asyncio.run(s.async_added_to_hass())
    assert polls == [ps.POLL] and tasks == ["nimbus_p5min_first_fetch"]
