"""`solver_inputs/price_blend.py`'s fallbacks and once-only warnings (#2393).

Main's CI put the file at 89%. The lines no test reached are the edges each
price reader promises never to fail at: no regional forecast, no history, no
bucket, no AEMO coverage past the retailer's own feed, and warnings that fire
once per stale episode or period, not every solve. Synthetic prices only.
"""

from __future__ import annotations

import logging
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from solver_inputs import price_blend

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)  # 16:00 Brisbane
FIVE = timedelta(minutes=5)


def bucket(t):
    local = price_blend.solver_shared._local(t)
    return local.hour * 12 + local.minute // 5


@pytest.mark.parametrize(
    "read",
    [
        KeyError("forecast"),
        urllib.error.HTTPError("http://ha", 500, "boom", None, None),
        {"attributes": {}},
    ],
    ids=["entity gone", "HTTP error", "no forecast attribute"],
)
def test_a_missing_or_unreadable_regional_forecast_is_no_forecast(monkeypatch, read):
    def ha_get(_entity_id):
        if isinstance(read, Exception):
            raise read
        return read

    monkeypatch.setattr(price_blend.solver_shared, "ha_get", ha_get)
    assert price_blend.fetch_aemo_forecast(None) == []
    assert price_blend.fetch_aemo_forecast("sensor.regional_forecast") == []


def test_the_risk_band_is_per_five_minute_of_day_and_falls_back_to_the_point_price():
    history = [(T0 - timedelta(days=1), 0.10), (T0 - timedelta(days=2), 0.30)]
    assert price_blend.compute_price_percentile_band([], 90) == {}
    band = price_blend.compute_price_percentile_band(history, 100)
    assert band == {bucket(T0): pytest.approx(0.30)}
    assert price_blend.compute_price_percentile_band(history, 0) == {
        bucket(T0): pytest.approx(0.10)
    }

    # No band at all is None, the documented "off"; a period with no bucket
    # keeps its own point price.
    assert price_blend.apply_price_band([0.2], [T0], {}) is None
    assert price_blend.apply_price_band([0.2, 0.25], [T0, T0 + FIVE], band) == [
        pytest.approx(0.30),
        0.25,
    ]


def test_past_its_own_feed_with_no_regional_forecast_the_last_real_price_holds():
    forecast = [
        {"time": T0.isoformat(), "per_kwh": 0.20},
        {"time": (T0 + FIVE).isoformat(), "per_kwh": 0.25},
    ]
    values, real = price_blend.resample_price_with_extrapolation(
        forecast, "per_kwh", [T0, T0 + FIVE, T0 + 2 * FIVE], [], {}
    )
    assert values == [0.20, 0.25, 0.25]
    assert real == [True, True, False]


def test_the_markup_and_the_live_check_need_history_and_a_bucket(monkeypatch):
    assert price_blend.compute_5min_offset([]) == {}
    # A wholesale price that cannot be scaled (no loss factor for that time)
    # is left out rather than guessed.
    monkeypatch.setattr(
        price_blend, "fetch_price_history", lambda *_a, **_k: [(T0, 0.10)]
    )
    assert (
        price_blend.compute_5min_offset(
            [(T0, 0.30)],
            regional_spot_sensor="sensor.regional_spot",
            wholesale_scale=lambda _start: None,
        )
        == {}
    )

    offset = {bucket(T0): 0.10}
    assert (
        price_blend.check_aemo_p5min_disagreement(None, 0.30, bucket(T0), offset, 0.05)
        is None
    )
    assert (
        price_blend.check_aemo_p5min_disagreement(
            "sensor.regional_spot", 0.30, bucket(T0) + 1, offset, 0.05
        )
        is None
    )


def test_warnings_fire_once_per_stale_episode_and_once_per_period(monkeypatch, caplog):
    monkeypatch.setattr(price_blend, "_STALE_PRICE_LAST_WARNED", None)
    monkeypatch.setattr(price_blend, "_AEMO_FC_LAST_WARNED_PERIOD", None)
    stale = {
        "entity_id": "sensor.retail_price",
        "source_updated": "2026-09-20T05:00:00+00:00",
        "age_minutes": 75.0,
        "stale_after_minutes": 60.0,
    }
    forecast = {
        "forecast_price": 0.30,
        "window_start": "16:00",
        "window_end": "16:30",
        "realised_mean_price": 0.10,
        "n_samples": 3,
        "disagreement_dollars": 0.20,
        "threshold_dollars": 0.05,
    }
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            price_blend._warn_stale_price_source_once(stale)
            price_blend._warn_aemo_forecast_vs_actuals_once(T0, forecast)
        price_blend._warn_stale_price_source_once(
            {**stale, "source_updated": "2026-09-20T07:00:00+00:00"}
        )
        price_blend._warn_aemo_forecast_vs_actuals_once(T0 + 6 * FIVE, forecast)

    messages = [r.getMessage() for r in caplog.records]
    assert sum("#1793" in m for m in messages) == 2
    assert sum("#452: AEMO's own 30-min forecast" in m for m in messages) == 2
