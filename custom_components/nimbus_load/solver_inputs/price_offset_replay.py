"""Replay the price consolidation's learned offsets over the stored history (#1786).

Mark Purcell, 1 Nov 2026: the blend-versus-source comparison the consolidation
`live` switch rests on was not like for like, and the offset meant to carry a
standing contract premium was following the last couple of hours. Before the
scoring changes, show the difference on this install's own history.

The input is `price_error_history`'s stored `(forecast, settled)` pairs per
side, source and lead bucket (21 days, one normal interval in `STRIDE`). Only
on-stride pairs are used, so every source is sampled the same way in time.

For each side, source and bucket, and each candidate `alpha`:

- `hindsight_mae`: the error today's tracker scores, with the offset learned
  up to the moment the interval settled.
- `fair_mae`: the error with the offset that was known when the forecast was
  issued (the bucket's representative lead before the interval), which is all
  the blend ever had.
- `offset_p10` / `offset_p90`: the spread of that offset over the history. A
  standing premium is flat.

`alpha` is per settled 5-minute interval, as the tracker's `EWMA_ALPHA` is. A
kept pair stands for `STRIDE` intervals, so each step uses `1 - (1 - alpha) **
STRIDE`.

`blend`, per side and bucket and alpha: the weighted sources rebuilt with the
fair offsets and inverse-fair-MAE weights, scored the same fair way, beside
the blend that was actually published (`as_published`).

Pure: no Home Assistant import, no I/O. Read-only by construction.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from . import price_consolidation as pc
    from . import price_error_history as peh
except ImportError:  # pragma: no cover - standalone/cron path
    import price_consolidation as pc  # type: ignore[no-redef]
    import price_error_history as peh  # type: ignore[no-redef]

#: Today's memory, then the candidates Mark asked to compare.
ALPHAS: tuple[float, ...] = (0.05, 0.005, 0.002, 0.001)
#: Kept pairs a source needs before its offset or weight is used, the
#: tracker's MIN_SAMPLES in strided pairs.
WARMUP = -(-pc.MIN_SAMPLES // peh.STRIDE)


def strided_alpha(alpha: float, stride: int = peh.STRIDE) -> float:
    return 1.0 - (1.0 - alpha) ** stride


def _pairs(classes: Mapping[str, Sequence[Sequence[float]]]) -> list[tuple]:
    out = [
        (int(s[0]), float(s[2]), float(s[3]))
        for cls in classes.values()
        for s in cls
        if peh.on_stride(int(s[0]))
    ]
    out.sort()
    return out


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))]


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 5)


def _offset_timeline(pairs: list[tuple], a: float) -> tuple[list[int], list[float]]:
    """(settled interval keys, the offset just after each), from WARMUP on."""
    keys: list[int] = []
    offsets: list[float] = []
    b: float | None = None
    for n, (key, forecast, settled) in enumerate(pairs, 1):
        signed = forecast - settled
        b = signed if b is None else (1 - a) * b + a * signed
        if n >= WARMUP:
            keys.append(key)
            offsets.append(b)
    return keys, offsets


def _known_at(keys: list[int], offsets: list[float], issued_key: float) -> float:
    i = bisect_right(keys, issued_key) - 1
    return offsets[i] if i >= 0 else 0.0


def _lead_intervals(bucket: str) -> float:
    return peh._rep_lead(bucket) / 5.0


def replay_source(pairs: list[tuple], bucket: str, alpha: float) -> dict[str, Any]:
    a = strided_alpha(alpha)
    lead = _lead_intervals(bucket)
    keys, offsets = _offset_timeline(pairs, a)
    hind: list[float] = []
    fair: list[float] = []
    b: float | None = None
    for n, (key, forecast, settled) in enumerate(pairs, 1):
        signed = forecast - settled
        used = b if (b is not None and n > WARMUP) else 0.0
        hind.append(abs(signed - used))
        fair.append(abs(signed - _known_at(keys, offsets, key - lead)))
        b = signed if b is None else (1 - a) * b + a * signed
    return {
        "n": len(pairs),
        "hindsight_mae": _r(sum(hind) / len(hind)) if hind else None,
        "fair_mae": _r(sum(fair) / len(fair)) if fair else None,
        "offset_p10": _r(_quantile(offsets, 0.1)),
        "offset_p90": _r(_quantile(offsets, 0.9)),
    }


def replay_blend(
    by_source: Mapping[str, list[tuple]], bucket: str, alpha: float
) -> dict[str, Any] | None:
    """The sources re-blended with fair offsets and inverse-fair-MAE weights,
    each known when the forecast was issued."""
    a = strided_alpha(alpha)
    lead = _lead_intervals(bucket)
    timelines = {s: _offset_timeline(p, a) for s, p in by_source.items()}
    # Running fair MAE per source, also as known at issue time.
    mae_lines: dict[str, tuple[list[int], list[float]]] = {}
    for s, pairs in by_source.items():
        keys, offs = timelines[s]
        mk: list[int] = []
        mv: list[float] = []
        m: float | None = None
        for n, (key, forecast, settled) in enumerate(pairs, 1):
            err = abs(forecast - settled - _known_at(keys, offs, key - lead))
            m = err if m is None else (1 - a) * m + a * err
            if n >= WARMUP:
                mk.append(key)
                mv.append(m)
        mae_lines[s] = (mk, mv)
    rows: dict[int, dict[str, tuple[float, float]]] = {}
    for s, pairs in by_source.items():
        for key, forecast, settled in pairs:
            rows.setdefault(key, {})[s] = (forecast, settled)
    errs: list[float] = []
    for key in sorted(rows):
        issued = key - lead
        num = den = 0.0
        settled = None
        for s, (forecast, st) in rows[key].items():
            mk, mv = mae_lines[s]
            i = bisect_right(mk, issued) - 1
            if i < 0:
                continue
            w = 1.0 / max(mv[i], 1e-4)
            num += w * (forecast - _known_at(*timelines[s], issued))
            den += w
            settled = st
        if den > 0 and settled is not None:
            errs.append(abs(num / den - settled))
    if not errs:
        return None
    return {"n": len(errs), "fair_mae": _r(sum(errs) / len(errs))}


def replay(
    samples: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
    alphas: Sequence[float] = ALPHAS,
) -> dict[str, Any]:
    """`samples` is `PriceErrorHistory.samples`. MAE and offsets in $/kWh."""
    out: dict[str, Any] = {"alphas": list(alphas), "warmup_pairs": WARMUP}
    for side in peh.SIDES:
        side_out: dict[str, Any] = {}
        sources = samples.get(side) or {}
        buckets = sorted({b for s in sources.values() for b in s})
        for src, by_bucket in sorted(sources.items()):
            side_out[src] = {
                b: {str(a): replay_source(_pairs(by_bucket[b]), b, a) for a in alphas}
                for b in sorted(by_bucket)
            }
        blend: dict[str, Any] = {}
        for b in buckets:
            parts = {
                s: _pairs(sources[s][b])
                for s in sources
                if s != pc.BLEND_SOURCE and b in sources[s]
            }
            published = _pairs((sources.get(pc.BLEND_SOURCE) or {}).get(b) or {})
            blend[b] = {
                "as_published_mae": _r(
                    sum(abs(f - st) for _k, f, st in published) / len(published)
                )
                if published
                else None,
                **{str(a): replay_blend(parts, b, a) for a in alphas},
            }
        side_out["blend"] = blend
        out[side] = side_out
    return out
