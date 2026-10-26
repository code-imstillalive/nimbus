"""Which load forecast the plan uses: the ML forecast, yesterday's load, or a
blend, decided from the trailing day-ahead record (#937) (#1837).

Off by default, and off changes nothing: the same array comes back and
neither the record nor the recorder is read. A policy that wants
persistence and cannot have a real baseline says so and keeps the ML
forecast. The baseline is the same time 24 h earlier, read from the
recorder once an hour. Synthetic data only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import solver_publish
import solver_shared as shared
import solver_writer as sw
from solver import forecast_source_selection as fss
from solver_inputs import load as load_inputs

BNE = timezone(timedelta(hours=10))
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=BNE)
LOSING = {f"2026-09-{d:02d}": -1.0 for d in range(5, 13)}  # persistence won every day
HOUSE = {"solver_whole_house_cross_check_sensor": "sensor.house"}


def _grid(n=8, minutes=15):
    return [NOW + timedelta(minutes=minutes * i) for i in range(n)]


@pytest.fixture
def site(monkeypatch):
    state = {
        "record": dict(LOSING),
        "reads": 0,
        "history": None,
        "fetches": 0,
        "scale": 1.0,
    }

    def record():
        state["reads"] += 1
        return dict(state["record"])

    def fetch(_e, _s, _t):
        state["fetches"] += 1
        return list(state["history"] or [])

    def nearest(pts, grid_times, period_hours):
        """Nearest at or before: these tests are about which instant each
        period looks up, which is the part the baseline owns."""
        ordered = sorted(pts, key=lambda tv: tv[0])
        out = []
        for gt in grid_times:
            value = 0.0
            for ts, v in ordered:
                if ts > gt:
                    break
                value = v
            out.append(value)
        return out

    monkeypatch.setattr(sw, "read_day_ahead_value_add_history", record)
    monkeypatch.setattr(shared, "fetch_entity_history_range", fetch)
    monkeypatch.setattr(shared, "resample_history_mean", nearest)
    monkeypatch.setattr(shared, "_kw_scale_factor", lambda _e: state["scale"])
    load_inputs._PERSISTENCE_HISTORY_CACHE.clear()
    yield state
    load_inputs._PERSISTENCE_HISTORY_CACHE.clear()


def _resolve(cfg, load, grid=None, hours=0.25):
    return load_inputs._resolve_load_forecast_source(
        cfg, grid or _grid(), hours, NOW, load
    )


def test_measure_reads_the_record_and_changes_nothing(site):
    load = [1.0] * 8
    out, decision, persistence = _resolve(
        {"solver_load_forecast_source_policy": "measure"}, load
    )
    assert out is load and persistence is None
    assert decision.reason == fss.REASON_PERSISTENCE_FAVOURED_MEASURE_ONLY
    assert (site["reads"], site["fetches"]) == (1, 0)


@pytest.mark.parametrize(
    ("cfg", "history"),
    [
        ({}, None),  # no whole-house sensor
        (HOUSE, []),  # an empty recorder read
        (
            HOUSE,
            [(NOW - timedelta(hours=h), 0.0) for h in range(24, 0, -1)],
        ),  # all zero is not a baseline
    ],
    ids=["no_sensor", "empty_history", "all_zero"],
)
def test_persistence_without_a_real_baseline_keeps_the_ml_forecast_and_says_why(
    site, caplog, cfg, history
):
    site["history"] = history
    load = [1.0] * 8
    with caplog.at_level("WARNING"):
        out, decision, persistence = _resolve(
            {"solver_load_forecast_source_policy": "persistence", **cfg}, load
        )
    assert out is load and persistence is None and decision.persistence_weight == 0.0
    if not cfg:
        assert decision.reason == "persistence_baseline_unavailable"
        assert (decision.days_scored, decision.days_persistence_won) == (
            len(LOSING),
            len(LOSING),
        )
        assert decision.mean_value_add_dollars is not None


def _yesterday(value=lambda h: float(h)):
    return [
        (NOW - timedelta(hours=24) + timedelta(hours=h), value(h)) for h in range(25)
    ]


def test_the_baseline_is_the_same_time_yesterday_and_the_day_before_past_24h(site):
    site["history"] = _yesterday()
    out = load_inputs._seasonal_naive_load_kw(
        HOUSE, [NOW + timedelta(hours=h) for h in range(6)], 1.0, NOW
    )
    assert (out[0], out[1], out[5]) == (0.0, 1.0, 5.0)
    load_inputs._PERSISTENCE_HISTORY_CACHE.clear()
    out = load_inputs._seasonal_naive_load_kw(
        HOUSE, [NOW + timedelta(hours=h) for h in (25, 30)], 1.0, NOW
    )
    assert out == [1.0, 6.0]


def test_the_baseline_is_read_once_an_hour_and_the_cache_stays_small(site):
    site["history"] = _yesterday()
    for minute in range(10):
        load_inputs._seasonal_naive_load_kw(
            HOUSE, _grid(), 0.25, NOW + timedelta(minutes=minute)
        )
    assert site["fetches"] == 1
    load_inputs._seasonal_naive_load_kw(HOUSE, _grid(), 0.25, NOW + timedelta(hours=1))
    assert site["fetches"] == 2
    for hour in range(24):
        load_inputs._seasonal_naive_load_kw(
            HOUSE, _grid(), 0.25, NOW + timedelta(hours=hour)
        )
    assert (
        len(load_inputs._PERSISTENCE_HISTORY_CACHE)
        <= load_inputs._PERSISTENCE_CACHE_MAX_ENTRIES
    )


def test_the_baseline_is_in_kw_and_never_negative(site):
    site["history"], site["scale"] = _yesterday(), 0.001
    assert load_inputs._seasonal_naive_load_kw(
        HOUSE, [NOW + timedelta(hours=1)], 1.0, NOW
    )[0] == pytest.approx(0.001)
    load_inputs._PERSISTENCE_HISTORY_CACHE.clear()
    site["history"], site["scale"] = _yesterday(lambda h: -5.0 if h == 3 else 2.0), 1.0
    out = load_inputs._seasonal_naive_load_kw(
        HOUSE, [NOW + timedelta(hours=h) for h in range(6)], 1.0, NOW
    )
    assert all(v >= 0.0 for v in out)


def test_a_persistence_policy_replaces_the_array_and_a_blend_lands_between(site):
    site["history"] = _yesterday(lambda h: 3.0)
    grid = [NOW + timedelta(hours=h) for h in range(4)]
    cfg = {"solver_load_forecast_source_policy": "persistence", **HOUSE}
    out, decision, persistence = _resolve(cfg, [9.0] * 4, grid, 1.0)
    assert decision.source == fss.SOURCE_PERSISTENCE and out == persistence == [3.0] * 4
    site["record"] = {
        **{f"2026-09-{d:02d}": -1.0 for d in range(5, 11)},
        "2026-09-11": 0.5,
        "2026-09-12": 0.5,
    }
    out, decision, _p = _resolve(
        {**cfg, "solver_load_forecast_source_policy": "blend"}, [11.0] * 4, grid, 1.0
    )
    assert decision.persistence_weight == pytest.approx(0.75) and out == pytest.approx(
        [5.0] * 4
    )


# --- what the plan publishes about it ---------------------------------------------------------


def test_the_decision_is_published_as_a_stable_set_of_scalars():
    keys = set(solver_publish._LOAD_FORECAST_SOURCE_KEYS)
    blank = solver_publish._load_forecast_source_attributes(None)
    assert set(blank) == keys and all(v is None for v in blank.values())
    today = datetime(2026, 9, 13, tzinfo=BNE).date()
    blend = fss.select_forecast_source(
        value_add_by_day=LOSING, today=today, policy=fss.POLICY_BLEND
    )
    out = solver_publish._load_forecast_source_attributes(blend)
    assert set(out) == keys and all(
        isinstance(v, (str, int, float)) for v in out.values()
    )
    assert (
        out["load_forecast_source_policy"],
        out["load_forecast_persistence_weight"],
    ) == ("blend", 1.0)
    rounded = solver_publish._load_forecast_source_attributes(
        fss.ForecastSourceDecision(
            source="blend",
            persistence_weight=11 / 14,
            policy="blend",
            reason="x",
            days_scored=14,
            days_persistence_won=11,
            mean_value_add_dollars=-0.712345678,
        )
    )
    assert (
        rounded["load_forecast_persistence_weight"],
        rounded["load_forecast_source_mean_value_add_dollars"],
    ) == (0.7857, -0.7123)
    empty = fss.select_forecast_source(
        value_add_by_day={}, today=today, policy="measure"
    )
    assert (
        solver_publish._load_forecast_source_attributes(empty)[
            "load_forecast_source_mean_value_add_dollars"
        ]
        is None
    )
