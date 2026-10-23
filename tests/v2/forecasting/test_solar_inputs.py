"""Every solar forecast source, read and blended into the solve's three
arrays (#542, #543, #546, #735, #2004) (#1837).

Each source is read in whichever shape it publishes -- a generic forecast,
Solcast's detailed forecast, Open-Meteo's watts -- and a source that cannot
be read is dropped, never zero-filled. Several sources are averaged, their
disagreement widening the band; a known integration is read once, never
also as a configured source; a live solar reading anchors the first
period; with nothing at all the solve still runs, flat at zero, and says so.
"""

from __future__ import annotations

import logging
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
import solver_shared as shared
import solver_writer as sw
from solver_inputs import solar

T0 = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
N = 4
GRID = [T0 + timedelta(hours=i) for i in range(N)]


def _forecast(values, lower=None, upper=None, step=timedelta(hours=1)):
    rows = []
    for i, v in enumerate(values):
        row = {"time": (T0 + step * i).isoformat(), "value": v}
        if lower is not None:
            row["lower"], row["upper"] = lower[i], upper[i]
        rows.append(row)
    return {"forecast": rows}


@pytest.fixture
def site(monkeypatch):
    states: dict = {}

    def get(eid):
        value = states[eid]
        if isinstance(value, Exception):
            raise value
        return value if "attributes" in value else {"state": "0", "attributes": value}

    monkeypatch.setattr(sw, "ha_get", get)
    monkeypatch.setattr(shared, "ha_get", get)  # the unit lookup reads it here
    monkeypatch.setattr(sw, "entity_exists", lambda eid: eid in states)
    monkeypatch.setattr(sw, "_SOLAR_SOURCE_WARNED", set())
    return states


def _arrays(cfg, **kw):
    return solar.build_solar_arrays(cfg, GRID, N, **kw)


def test_one_source_keeps_its_own_band_clamped_to_bracket_it(site):
    site["sensor.a"] = _forecast(
        [-0.5, 2.0, 2.0, 2.0], lower=[3.0, 1.0, 1.0, 1.0], upper=[1.0, 3.0, 3.0, 3.0]
    )
    kw, lo, up = _arrays({"solver_solar_forecast_sensor": "sensor.a"})
    assert kw == [0.0, 2.0, 2.0, 2.0]  # no negative solar
    assert all(lo[i] <= kw[i] <= up[i] for i in range(N))
    assert (lo[1], up[1]) == (1.0, 3.0)


def test_sources_are_averaged_and_their_disagreement_widens_the_band(site):
    site["sensor.a"] = _forecast([2.0] * N)
    site["sensor.b"] = _forecast([2.0] * N)
    site["sensor.c"] = _forecast([4.0] * N)
    agree, lo, up = _arrays(
        {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_forecast_sensor_2": "sensor.b",
        }
    )
    assert agree == pytest.approx([2.0] * N) and lo == pytest.approx(agree) == up
    kw, lo, up = _arrays(
        {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_forecast_sensor_2": "sensor.c",
        }
    )
    assert kw == pytest.approx([3.0] * N)
    assert all(lo[i] < kw[i] < up[i] for i in range(N))


@pytest.mark.parametrize(
    "broken",
    [{"something_else": 1}, urllib.error.URLError("down")],
    ids=["unknown_shape", "unreachable"],
)
def test_a_source_that_cannot_be_read_is_dropped_and_said_once(site, caplog, broken):
    site["sensor.bad"] = broken
    site["sensor.good"] = _forecast([4.0] * N)
    cfg = {
        "solver_solar_forecast_sensor": "sensor.bad",
        "solver_solar_forecast_sensor_2": "sensor.good",
    }
    with caplog.at_level(logging.WARNING):
        assert _arrays(cfg)[0] == [4.0] * N  # never averaged in as zeros
        _arrays(cfg)
    assert caplog.text.count("sensor.bad") == 1


def test_with_no_source_at_all_the_solve_runs_flat_at_zero_and_says_so(site, caplog):
    with caplog.at_level(logging.WARNING):
        arrays = _arrays({})
    assert arrays == ([0.0] * N, [0.0] * N, [0.0] * N)
    assert all(isinstance(v, float) for a in arrays for v in a)
    assert "no solar forecast source" in caplog.text


@pytest.mark.parametrize(
    ("reading", "unit", "first"),
    [("3800", "W", 3.8), ("3.8", "kW", 3.8), ("-1", "kW", 0.0), ("unknown", "kW", 2.0)],
)
def test_a_live_solar_reading_anchors_the_first_period_only(site, reading, unit, first):
    site["sensor.a"] = _forecast([2.0] * N)
    site["sensor.pv_power"] = {
        "state": reading,
        "attributes": {"unit_of_measurement": unit},
    }
    kw, lo, up = _arrays(
        {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_power_sensor": "sensor.pv_power",
        }
    )
    assert (kw[0], lo[0], up[0]) == pytest.approx((first, first, first))
    assert kw[1:] == [2.0] * (N - 1)


def _solcast(values, low, high):
    return {
        "detailedForecast": [
            {
                "period_start": (T0 + timedelta(hours=i)).isoformat(),
                "pv_estimate": v,
                "pv_estimate10": lo,
                "pv_estimate90": hi,
            }
            for i, (v, lo, hi) in enumerate(zip(values, low, high, strict=True))
        ]
    }


def test_solcast_is_read_with_its_own_p10_and_p90(site):
    site["sensor.solcast_today"] = _solcast([3.0] * N, [2.0] * N, [4.0] * N)
    kw, lo, up = _arrays({"solver_solar_forecast_sensor": "sensor.solcast_today"})
    assert (kw, lo, up) == ([3.0] * N, [2.0] * N, [4.0] * N)


def test_open_meteo_watts_are_instants_read_where_each_period_sits():
    """#2004: watts[t] is the power at t + 15 minutes, an instant -- never a
    value held from its key."""
    key = T0
    entries = sw._solar_entries_from_attributes({"watts": {key.isoformat(): 1500}})
    assert entries == [
        {"time": key + sw.OPEN_METEO_WATTS_LEAD, "value": 1.5, "instant": True}
    ]
    assert sw.OPEN_METEO_WATTS_LEAD == timedelta(minutes=15)
    assert solar._resampler(entries) is sw.resample_instants
    assert solar._resampler([{"time": key, "value": 1}]) is sw.resample_forecast


KNOWN = {
    "known_sources": {
        "solcast": ["sensor.solcast_today", "sensor.solcast_tomorrow"],
        "open_meteo": ["sensor.om_today", "sensor.om_tomorrow"],
    }
}


def test_a_known_integration_is_read_once_never_also_as_a_configured_source(site):
    """#546: configuring Solcast's today entity as a source as well as
    auto-including Solcast would read it twice and drag the blend."""
    site["sensor.solcast_today"] = _solcast([6.0] * N, [5.0] * N, [7.0] * N)
    site["sensor.solcast_tomorrow"] = {"detailedForecast": []}
    cfg = {
        **KNOWN,
        "solver_auto_include_known_solar": True,
        "solver_solar_forecast_sensor": "sensor.solcast_today",
    }
    band = []
    kw, _lo, _up = _arrays(cfg, solcast_band_out=band)
    assert kw == [6.0] * N
    assert band == [([6.0] * N, [5.0] * N, [7.0] * N)]  # as fetched, for the quantiles
    off = {**cfg, "solver_auto_include_known_solar": False}
    assert _arrays(off)[0] == [6.0] * N  # read as a plain source instead


def test_auto_included_open_meteo_joins_the_blend(site):
    def watts(start_hours, kw):
        return {
            "watts": {
                (T0 + timedelta(hours=h)).isoformat(): kw * 1000
                for h in range(start_hours, start_hours + 6)
            }
        }

    site["sensor.om_today"] = watts(-1, 2.0)
    site["sensor.om_tomorrow"] = watts(5, 2.0)
    site["sensor.a"] = _forecast([4.0] * N)
    cfg = {
        **KNOWN,
        "solver_auto_include_known_solar": True,
        "solver_solar_forecast_sensor": "sensor.a",
    }
    kw, lo, up = _arrays(cfg)
    assert kw == pytest.approx([3.0] * N)
    assert all(lo[i] < kw[i] < up[i] for i in range(N))
    del site["sensor.om_today"]  # the anchor entity gone: Open-Meteo is absent
    assert _arrays(cfg)[0] == [4.0] * N


def test_a_source_that_comes_back_is_announced_and_warned_about_afresh_if_lost_again(
    site, caplog
):
    cfg = {"solver_solar_forecast_sensor": "sensor.a"}
    with caplog.at_level(logging.INFO):
        site["sensor.a"] = {"nothing": 1}
        _arrays(cfg)
        site["sensor.a"] = _forecast([2.0] * N)
        _arrays(cfg)
        _arrays(cfg)  # once back, nothing more to say
        site["sensor.a"] = {"nothing": 1}
        _arrays(cfg)
    said = [r.levelname for r in caplog.records if "sensor.a" in r.getMessage()]
    assert said == ["WARNING", "INFO", "WARNING"]
