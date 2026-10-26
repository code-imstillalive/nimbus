"""`build_price_arrays`'s fallbacks, through the real function (#1837).

The coverage-by-risk map put `solver_inputs/prices.py` third among tier 1's
gaps (76.5% of lines). A CI coverage run named the blocks no test reached:
- the stale price source fallback (#1793, Mark Purcell): a primary whose
  rows have not been rebuilt for over an hour is not planned on, and every
  period comes from AEMO plus the learned markup, with the live spot for
  the current interval;
- the shadow consolidation sources (#1786): AEMO plus markup, and the retail
  price from the RRP, must never break the solve when they fail.
Each test drives the real `build_price_arrays`; only the writer's readers
are fakes.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
import solver_writer as sw
from solver_inputs import price_consolidation, prices

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)  # 16:00 Brisbane
N = 3
GRID = [T0 + timedelta(minutes=5 * i) for i in range(N)]
PRIMARY = ([0.20] * N, [True] * N)
AEMO_IMPORT = [0.31, 0.32, 0.33]
AEMO_EXPORT = [0.11, 0.12, 0.13]
STALE = {"stale": True, "age_minutes": 75.0, "source": "sensor.array"}


@pytest.fixture
def writer(monkeypatch):
    """The writer's readers as fakes, and a record of the fallback's calls."""
    seen = {"live_spot": [], "warned": []}
    for name, value in {
        "ha_get": lambda e: {"state": "1", "attributes": {"forecast": []}},
        "fetch_aemo_forecast": lambda _e: None,
        "fetch_price_history": lambda *a, **k: [],
        "compute_5min_offset": lambda *a, **k: {},
        "compute_price_percentile_band": lambda *a, **k: {},
        "resample_price_with_extrapolation": lambda *a, **k: PRIMARY,
        "check_aemo_p5min_disagreement": lambda *a, **k: None,
        "p2p_match_fraction": lambda **k: 0.65,
        "p2p_recent_avg_volume_kwh": lambda **k: 60.0,
        "_warn_stale_price_source_once": lambda f: seen["warned"].append(f),
    }.items():
        monkeypatch.setattr(sw, name, value)
    return seen


def _fallback(seen, imp=AEMO_IMPORT, exp=AEMO_EXPORT):
    calls = iter([imp, exp])

    def aemo_series(grid_times, forecast, offsets, local, live_spot):
        seen["live_spot"].append(live_spot)
        return next(calls)

    return aemo_series


def _build(**cfg):
    return prices.build_price_arrays(
        {
            "solver_import_price_sensor": "sensor.i",
            "solver_export_price_sensor": "sensor.e",
            **cfg,
        },
        GRID,
        N,
        GRID[0],
        True,
        "sensor.array",
    )


def test_without_a_live_spot_the_fallback_still_runs(writer, monkeypatch):
    for price_now in [lambda eid, fallback: float("nan"), lambda eid, fallback: 1 / 0]:
        with monkeypatch.context() as mp:
            mp.setattr(prices.price_freshness, "check", lambda *a, **k: dict(STALE))
            mp.setattr(prices.price_freshness, "aemo_series", _fallback(writer))
            mp.setattr(sw, "price_now", price_now)
            out = _build(solver_regional_spot_current_price_sensor="sensor.spot")
            assert out.spot_import_raw == AEMO_IMPORT
            assert writer["live_spot"] == [None, None]
            assert not any(
                isinstance(v, float) and math.isnan(v) for v in writer["live_spot"]
            )


def test_with_no_aemo_series_the_stale_primary_is_kept_and_says_so(writer, monkeypatch):
    monkeypatch.setattr(prices.price_freshness, "check", lambda *a, **k: dict(STALE))
    monkeypatch.setattr(
        prices.price_freshness, "aemo_series", _fallback(writer, imp=None)
    )
    out = _build()
    assert out.spot_import_raw == PRIMARY[0]
    assert out.import_real_mask == PRIMARY[1]
    assert out.price_source_freshness["fallback"] is None
    assert writer["warned"] == []


def test_a_failing_consolidation_source_never_breaks_the_build(writer, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bad AEMO data")

    monkeypatch.setattr(prices.price_freshness, "check", lambda *a, **k: None)
    monkeypatch.setattr(price_consolidation, "aemo_markup_series", broken)
    monkeypatch.setattr(prices, "_retail_from_rrp", broken)
    out = _build()
    assert out.spot_import_raw == PRIMARY[0]
    assert out.aemo_import_series is None
    assert out.aemo_export_series is None
    assert out.aemo_source == "aemo_markup"
    assert out.loss_factor is None
