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

`blend_staleness`, per side and bucket, at today's memory: the same
rebuilt blend with a source's weight discounted the longer it has gone
unscored at that lead (Mark Purcell, 2 Nov 2026). `inflate k` scores it
as `mae * (1 + k * hours since its last sample)`; `uniform H` fades its
share toward an equal split over H hours; `none` is today. Each gives the
fair MAE and every source's mean share of the blend.

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
#: Mark Purcell, 2 Nov 2026 (#1786): a source scored rarely at a lead keeps
#: an old error, and so its weight, until it is scored again. Today's
#: weights, his inflation shape at three k (per hour), and a fade toward
#: an equal share at three horizons (hours).
STALENESS: tuple[tuple[str, float], ...] = (
    ("none", 0.0),
    ("inflate", 0.01),
    ("inflate", 0.02),
    ("inflate", 0.05),
    ("uniform", 6.0),
    ("uniform", 24.0),
    ("uniform", 72.0),
)

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
    by_source: Mapping[str, list[tuple]],
    bucket: str,
    alpha: float,
    staleness: tuple[str, float] = ("none", 0.0),
) -> dict[str, Any] | None:
    """The sources re-blended with fair offsets and inverse-fair-MAE weights,
    each known when the forecast was issued. `staleness` discounts a
    source's weight by the hours since its last sample at that point (see
    STALENESS)."""
    mode, amount = staleness
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
    share_sum: dict[str, float] = {}
    for key in sorted(rows):
        issued = key - lead
        # (source, inverse-MAE weight, fade toward equal, adjusted forecast)
        parts: list[tuple[str, float, float, float]] = []
        settled = None
        for s, (forecast, st) in rows[key].items():
            mk, mv = mae_lines[s]
            i = bisect_right(mk, issued) - 1
            if i < 0:
                continue
            hours = (issued - mk[i]) * 5.0 / 60.0
            mae = max(mv[i], 1e-4)
            fade = 0.0
            if mode == "inflate":
                mae *= 1.0 + amount * hours
            elif mode == "uniform":
                fade = min(1.0, hours / amount)
            adjusted = forecast - _known_at(*timelines[s], issued)
            parts.append((s, 1.0 / mae, fade, adjusted))
            settled = st
        if not parts or settled is None:
            continue
        total = sum(w for _s, w, _f, _a in parts)
        shares = {s: (1.0 - f) * w / total + f / len(parts) for s, w, f, _a in parts}
        norm = sum(shares.values())
        blended = sum(shares[s] * a for s, _w, _f, a in parts) / norm
        errs.append(abs(blended - settled))
        for s, share in shares.items():
            share_sum[s] = share_sum.get(s, 0.0) + share / norm
    if not errs:
        return None
    return {
        "n": len(errs),
        "fair_mae": _r(sum(errs) / len(errs)),
        "mean_share": {s: _r(v / len(errs)) for s, v in sorted(share_sum.items())},
    }


def replay(
    samples: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
    alphas: Sequence[float] = ALPHAS,
    blend_sources: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
) -> dict[str, Any]:
    """`samples` is `PriceErrorHistory.samples`. MAE and offsets in $/kWh.
    `blend_sources[side][bucket]` limits the rebuilt blend to the sources the
    published blend weights at that lead (its `weights`): the history holds
    sources the blend leaves out, at some leads or altogether. A side or
    bucket not given uses every source."""
    out: dict[str, Any] = {
        "alphas": list(alphas),
        "warmup_pairs": WARMUP,
        "blend_sources": {
            side: {b: list(v) for b, v in by_b.items()}
            for side, by_b in (blend_sources or {}).items()
        },
    }
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
                if s != pc.BLEND_SOURCE
                and b in sources[s]
                and (
                    b not in ((blend_sources or {}).get(side) or {})
                    or s in ((blend_sources or {}).get(side) or {})[b]
                )
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
        side_out["blend_staleness"] = {
            b: {
                (f"{mode} {amount:g}" if mode != "none" else "none"): replay_blend(
                    {
                        s: _pairs(sources[s][b])
                        for s in sources
                        if s != pc.BLEND_SOURCE
                        and b in sources[s]
                        and (
                            b not in ((blend_sources or {}).get(side) or {})
                            or s in ((blend_sources or {}).get(side) or {})[b]
                        )
                    },
                    b,
                    alphas[0],
                    staleness=(mode, amount),
                )
                for mode, amount in STALENESS
            }
            for b in buckets
        }
        out[side] = side_out
    return out
