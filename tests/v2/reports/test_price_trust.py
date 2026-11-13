"""A forecast priced past the configured source's own rows counts for more the
closer it gets (#2534, household decision 13 Nov 2026). Synthetic values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from solver_inputs import price_error_history as peh
from solver_inputs import price_trust as pt

NOW = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)


def _slot(t: datetime) -> int:
    return t.hour * 12 + t.minute // 5


def test_a_forecast_counts_for_more_the_closer_it_gets():
    typical = {s: 0.30 for s in range(288)}
    spread = {s: 0.05 for s in range(288)}
    times = [
        NOW + timedelta(minutes=30),
        NOW + timedelta(hours=6),
        NOW + timedelta(hours=27),
    ]
    spike = [1.28] * 3
    kw = {
        "typical": typical,
        "spread": spread,
        "threshold": 0.85,
        "buckets": peh.LEAD_BUCKETS,
        "bucket_5min": _slot,
    }

    # No graded spikes yet: the lead prior alone. Close, nearly the whole
    # spike; six hours, half of it; a day out, under a fifth.
    out, w = pt.apply(spike, ["secondary"] * 3, times, NOW, side_summary={}, **kw)
    assert w[0] > 0.9 and abs(w[1] - 0.5) < 1e-9 and w[2] < 0.2
    assert out[0] > out[1] > out[2] > 0.30
    assert abs(out[1] - (0.30 + 0.5 * (1.28 - 0.30))) < 1e-9
    # The same for a stale primary priced from AEMO, and for the extension.
    assert (
        pt.apply(spike, ["aemo_fallback"] * 3, times, NOW, side_summary={}, **kw)[1]
        == w
    )
    assert pt.apply(spike, ["fallback"] * 3, times, NOW, side_summary={}, **kw)[1] == w

    # The source's own rows, the settled block and live consolidation are
    # never touched.
    for label in ("primary", "consolidated"):
        same, none = pt.apply(spike, [label] * 3, times, NOW, side_summary={}, **kw)
        assert same == spike and none == [None] * 3

    # Measured: forecast spikes at a day out that all missed pull the weight
    # down; ones that all settled as spikes push it up.
    missed = {"secondary_3": {">24 h": {"spike": [0, 20, 0, 0.05]}}}
    hit = {"secondary_3": {">24 h": {"spike": [20, 0, 0, 0.95]}}}
    assert (
        pt.apply(spike, ["secondary"] * 3, times, NOW, side_summary=missed, **kw)[1][2]
        < w[2]
    )
    assert (
        pt.apply(spike, ["secondary"] * 3, times, NOW, side_summary=hit, **kw)[1][2]
        > 0.9
    )

    # Below the threshold: unchanged until the source's error has been
    # measured; a wide measured miss moves it towards the usual price.
    normal = [0.60] * 3
    assert (
        pt.apply(normal, ["secondary"] * 3, times, NOW, side_summary={}, **kw)[0]
        == normal
    )
    wide = {
        "secondary_3": {
            b[0]: {"below": [100, 5, -0.30, 0.0, 0.30]} for b in peh.LEAD_BUCKETS
        }
    }
    moved, wn = pt.apply(normal, ["secondary"] * 3, times, NOW, side_summary=wide, **kw)
    assert all(0.30 < v < 0.60 for v in moved) and all(
        x is not None and x < 1 for x in wn
    )

    # No usual price for that time of day: left as it is.
    kw_none = {**kw, "typical": {}}
    assert (
        pt.apply(spike, ["secondary"] * 3, times, NOW, side_summary={}, **kw_none)[0]
        == spike
    )

    # The usual price is the median at each 5-minute of day.
    hist = [
        (NOW + timedelta(days=d), v) for d, v in enumerate([0.2, 0.4, 0.3, 9.0, 0.25])
    ]
    typ, spr = pt.typical_and_spread(hist, _slot)
    assert typ[_slot(NOW)] == 0.3 and spr[_slot(NOW)] > 0
