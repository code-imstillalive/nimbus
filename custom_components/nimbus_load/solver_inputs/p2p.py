"""P2P (peer-to-peer) rate inputs, moved from `solver_writer.py` (#2289 item 6, step 8 part 1).

Reads the retailer's P2P rate feed (rate rows or raw triples), resamples it
onto the solve grid, and estimates the share of export that actually
matches. Called by `solver_inputs/prices.py` through this module, and by
`solver_writer` the same way. Every shared helper (`ha_get`, `NATIVE`,
`parse_iso`, `_LOGGER`) is reached through `solver_shared` by module, so a
test patches `solver_shared.<name>`; this module adds no late-import seam
back into `solver_writer`.
"""

from __future__ import annotations

import json
import math
import statistics
import urllib.error
from datetime import UTC, datetime, timedelta

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


# Fallback match fraction when the confirmed-history sensor is unavailable
# (about the 13-day average, 2026-08-16). Reported only: the LP is priced by
# export_bonus_price / export_bonus_volume_kwh below.
P2P_MATCH_FRACTION_FALLBACK = 0.65


# Fallback for the export bonus's volume cap when p2p_recent_avg_volume_kwh()
# has no history.
P2P_RECENT_AVG_VOLUME_FALLBACK_KWH = 60.0


def resample_real_p2p_rate(
    grid_times: list[datetime],
    sensor_id: str | None = None,
    p2p_window_kw: list[float] | None = None,
    fallback_sensor_id: str | None = None,
) -> list[float]:
    """Real, per-interval P2P export rate ($/kWh) -- REPLACES the old
    resample_p2p_forecast()/sensor.localvolts_p2p_price_forecast flat-
    $0.50-placeholder approach entirely (2026-08-20, direct household
    finding: "50c is an arbitrary unit we used to make HAEO believe...
    IT IS NOT THE PRICE IT ACTUALLY IS... actual price can vary from
    0-70c"). That placeholder was purpose-built as a workaround for
    HAEO's own specific LP-degeneracy problems with live per-period P2P
    data (seen on the reference household) -- HAEO still genuinely needs it, and sensor.localvolts_p2p_
    price_forecast / lv_p2p_forecast_writer.py are UNCHANGED and
    deliberately left alone. Nimbus is a different system with its own
    stability mechanisms (proximal_weight, smoothness_weight) and has no
    reason to inherit a workaround built for a different LP's problems.

    Rate formula -- EXACTLY matches this project's own already-correct
    "P2P Trades Tonight" card (the reference household's live card's
    own Jinja):
        rate = matchedCost / (volume * proportionP2P)
    (that card displays cents; this returns dollars -- matched_vol is in
    kWh, matchedCost in $, so cost/matched_vol is already $/kWh directly,
    no *100/100 round-trip needed). Sourced from `sensor_id`
    (CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR, was hardcoded to
    sensor.localvolts_p2p_forecast until the 2026-09-02 audit -- a
    genuinely different sensor from the flat placeholder above -- this
    one carries real per-5-min matchedCost/volume/proportionP2P from
    LocalVolts' own live matching, the same real data the household's
    own reference card already uses).

    Verified live 2026-08-20, before building this, not assumed safe:
    (1) real economic plausibility -- forecast-quality (not yet settled)
    rates hours ahead showed genuine variation (43-65c), not a frozen
    template; (2) poll-to-poll stability -- the same future intervals,
    checked via two live polls 5 minutes apart, came back byte-identical
    (no drift/noise). That two-part check is what justifies feeding this
    directly into an LP as a live price signal, where the OLD flat-
    placeholder hack existed specifically because raw per-period P2P data
    was NOT trustworthy enough for HAEO's own architecture.

    Real points are keyed by INTERVAL START (time - 5min) -- LocalVolts
    labels every interval by its END, the same end-vs-start convention
    this project has hit and fixed more than once before for this exact
    sensor; matches the household's own reference card's own `ts = end_ts
    - timedelta(minutes=5)`. A period with ~0 real matched volume (most
    of the day, outside the real P2P window) contributes rate=0.0, same
    shape as the old placeholder's own zero-outside-window behaviour.

    Real coverage confirmed live 2026-08-20 to run out ~14h ahead (much
    shorter than the old placeholder's ~36h, since this is genuine live
    matching data, not a repeating template) -- beyond it, falls back to
    the MEDIAN of every real, meaningfully-matched rate seen within
    coverage, applied only during the real 17:00-24:00 local P2P window
    (median, not mean/max, specifically so one real outlier interval --
    e.g. a partial-minute settlement blip -- can't skew every future
    day's whole-window assumption). Same explicit 17<=hour<24 gate
    applied uniformly to BOTH branches (not just the extrapolated one) --
    this project already found and fixed a real bug once before
    (2026-08-17) where a stray nonzero in-coverage reading leaked outside
    the real window because only the extrapolation branch had the gate;
    not repeating that mistake here.

    The window (nimbus #1537 item 5): `p2p_window_kw` is the household's
    own configured blocks, from `fetch_p2p_fixed_export_kw()`, and a period
    is inside the window when its value there is > 0; beyond the forecast's
    coverage a block keeps the median real matched rate.

    With NO block configured (`p2p_window_kw` None) nothing is hardcoded
    (#1559): the rate is flexible, counting in every interval the forecast
    itself shows matched, and 0 beyond the forecast's coverage, since
    without a commitment there is nothing to extrapolate. The paragraph
    above about "the explicit 17<=hour<24 gate" describes the reference
    household's original window; it applied to every install, with or
    without blocks, until v0.94.439 (blocks) and #1559 (no blocks).

    Returns a flat 0.0 array (never crashes) if `sensor_id` is blank --
    the same graceful no-op every household with no P2P/community-
    trading program at all gets.

    Two row shapes (nimbus #1537 item 3), detected from the rows: the raw
    triple above, and `{time, value}` rows whose value IS the matched rate
    per kWh, stamped at the interval START, accepted only with a per-kWh
    unit and a known interval (see `_p2p_source_observations`).
    `fallback_sensor_id` (CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2,
    a transitional binding) is an optional second source; the conflict and
    expiry rules are in `_resolve_p2p_observations` and
    P2P_RATE_SOURCE_MAX_AGE.
    """
    # nimbus #1537 item 3. Two paths, deliberately:
    #
    # * ONE raw-triple source (every install configured before #1537,
    #   including the reference household's live one): the pre-#1537
    #   algorithm, unchanged to the byte (its step lookup is pinned in
    #   tests/v2/orchestration/test_writer_inputs.py).
    # * Anything else -- a `{time, value}` source, or two sources -- goes
    #   through the observation model below, which keeps each interval's own
    #   validity and coverage instead of collapsing "missing", "stale",
    #   "explicitly unmatched" and "matched at $0" into the same 0.0 (Mark
    #   Purcell's review of PR #1592).
    sources = [s for s in (sensor_id, fallback_sensor_id) if s]
    if len(sources) == 2 and sources[0] == sources[1]:
        sources = sources[:1]
    if not sources:
        return [0.0 for _ in grid_times]
    states = [_read_p2p_state(s) for s in sources]
    if len(sources) == 1:
        state = states[0]
        if state is None:
            return [0.0 for _ in grid_times]
        raw = state["attributes"].get("forecast")
        raw = raw if isinstance(raw, list) else []
        if not _p2p_rows_are_rate_shape(raw):
            return _resample_p2p_points(
                _p2p_points_from_triple(raw), grid_times, p2p_window_kw
            )
    now = grid_times[0] if grid_times else None
    observed = [
        _p2p_source_observations(st, sid, now)
        for sid, st in zip(sources, states, strict=True)
    ]
    return _resolve_p2p_observations(observed, grid_times, p2p_window_kw)


# The rate shape's interval when a feed holds a single row (LocalVolts' own
# interval, and the same 5 minutes the raw triple's end-to-start shift uses).
_P2P_DEFAULT_INTERVAL = timedelta(minutes=5)


# A source whose snapshot is older than this, measured from the first plan
# period, is EXPIRED: it neither decides an interval nor feeds the block
# median. LocalVolts v2 polls every 5 minutes, so an hour is twelve missed
# polls. Applies on the observation path only; the single raw-triple path is
# frozen at its pre-#1537 behaviour (see resample_real_p2p_rate).
P2P_RATE_SOURCE_MAX_AGE = timedelta(hours=1)


# Per-interval observation states. UNAVAILABLE and UNCOVERED carry no
# information about the interval; NO_MATCH and RATE are the provider's own
# answer for it. A RATE of 0.0 is a genuine zero-priced match, not missing.
P2P_UNAVAILABLE = "unavailable"


P2P_UNCOVERED = "uncovered"


P2P_NO_MATCH = "no_match"


P2P_RATE = "rate"


def _p2p_rows_are_rate_shape(raw: list) -> bool:
    """True when a matched-rate feed is `{time, value}` rows rather than the
    raw triple (`matchedCost` / `volume` / `proportionP2P`, interval-END
    stamped).

    Decided from the rows, never the entity name (nimbus #1537). A feed whose
    rows carry any raw-triple field is the triple, exactly as before; only a
    feed with none of them and a `value` is read as rates, so every feed the
    pre-#1537 code read keeps being read the same way."""
    rows = [p for p in raw if isinstance(p, dict)]
    if any(("matchedCost" in p or "proportionP2P" in p or "volume" in p) for p in rows):
        return False
    return any("value" in p for p in rows)


def _p2p_points_from_triple(raw: list) -> list[tuple[datetime, float]]:
    pts = []
    for p in raw:
        try:
            # nimbus #1537: purcell-lab/localvolts_v2 (>= 2.7.0) publishes the
            # same Sell rows with `matchedCost`, but keys the interval end as
            # `intervalEnd` rather than the reference household's renamed
            # `time`. Both are the interval END, so the arithmetic below is
            # unchanged; `time` wins when a row carries both.
            end_t = solver_shared.parse_iso(
                p["time"] if "time" in p else p["intervalEnd"]
            )
            start_t = end_t - timedelta(minutes=5)
            vol = float(p.get("volume") or 0.0)
            prop = float(p.get("proportionP2P") or 0.0)
            cost = float(p.get("matchedCost") or 0.0)
            matched_vol = vol * prop
            rate = (cost / matched_vol) if matched_vol > 0.01 else 0.0
            pts.append((start_t, rate))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    return pts


def _resample_p2p_points(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    p2p_window_kw: list[float] | None,
) -> list[float]:
    """The pre-#1537 step lookup, for the single raw-triple path only."""
    if not pts:
        return [0.0 for _ in grid_times]

    last_real_time = pts[-1][0]
    real_positive_rates = [r for _, r in pts if r > 0.0]
    fallback_rate = (
        statistics.median(real_positive_rates) if real_positive_rates else 0.0
    )

    use_blocks = p2p_window_kw is not None and len(p2p_window_kw) == len(grid_times)
    out = []
    for i, gt in enumerate(grid_times):
        # Blocks configured: the rate counts inside them only. No block
        # configured: a flexible rate, every interval LocalVolts' own
        # forecast shows matched (#1559 -- no hardcoded hours).
        in_window = p2p_window_kw[i] > 0 if use_blocks else True  # NaN is False
        if not in_window:
            out.append(0.0)
        elif gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
        elif use_blocks:
            # Past the forecast's own coverage, a committed block still
            # sells at its usual rate: the median of real matched rates.
            out.append(float(fallback_rate))
        else:
            # Without a block there is no commitment to extrapolate, so no
            # P2P is assumed beyond what the forecast itself shows.
            out.append(0.0)
    return out


def _read_p2p_state(sensor_id: str) -> dict | None:
    """The source's HA state, or None when it cannot be read (UNAVAILABLE)."""
    try:
        state = solver_shared.ha_get(sensor_id)
        if not isinstance(state.get("attributes"), dict):
            raise TypeError("attributes is not a mapping")
        return state
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace solver_shared._LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # nimbus issue #363 (Mark Purcell, codebase review): the degrade-
        # to-flat-0.0 behaviour stays, but this used to have zero
        # breadcrumb -- a genuinely misconfigured/renamed P2P sensor would
        # otherwise silently price every period as if no P2P program
        # existed at all, with no way to tell that apart from "genuinely
        # no P2P configured."
        solver_shared._LOGGER.debug(
            "Nimbus Solver: resample_real_p2p_rate(%s) failed", sensor_id, exc_info=True
        )
        return None


def _p2p_snapshot_time(state: dict) -> datetime | None:
    """When the source's data was produced. The provider's own timestamp
    where the source publishes one (LocalVolts v2's Current Sell/Buy Rate
    carry `lastUpdate`, the provider's time on the current record); else the
    entity's `last_updated`, which says only when HA last saw the entity
    change and is the weaker of the two. None when neither parses."""
    attrs = state.get("attributes") or {}
    for raw in (attrs.get("lastUpdate"), state.get("last_updated")):
        if not raw:
            continue
        try:
            when = solver_shared.parse_iso(raw)
        except (AttributeError, TypeError, ValueError):
            continue
        return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return None


def _p2p_is_localvolts_matched_rate_feed(attrs: dict) -> bool:
    """LocalVolts v2's Sell/Buy P2P Matched Cost (`haeo_feed.py`), recognised
    by the attributes it publishes, not its entity name. Its contract,
    from that source: one row per 5-minute interval stamped at the interval
    START, value `matchedCost / (volume x proportionP2P)` in $/kWh, and an
    interval with no matched energy is OMITTED. So within the feed's own
    horizon a missing row is an explicit no-match -- a rule that holds for
    this feed only, never for `{time, value}` rows in general."""
    return (
        attrs.get("source_field") == "matchedCost"
        and attrs.get("interpolation_mode") == "previous"
    )


def _p2p_unit_scale(unit: object) -> float | None:
    """The factor taking a `<money>/kWh` unit to the household's major unit
    per kWh, or None when the unit is not a per-kWh price.

    `c/kWh` and `¢/kWh` are minor units (x 0.01). A single currency symbol
    (LocalVolts v2 publishes one, HAEO's own convention) or the household's
    ISO currency code is the major unit (x 1). No unit, `kW`, `%`, a bare
    currency, or another currency's code is refused: the row shape alone
    does not establish currency or scale."""
    text = str(unit or "").strip()
    if not text.lower().endswith("/kwh"):
        return None
    prefix = text[: -len("/kwh")].strip()
    if prefix.lower() in ("c", "¢"):
        return 0.01
    if len(prefix) == 1 and not prefix.isalnum() and not prefix.isspace():
        return 1.0
    household = getattr(
        getattr(solver_shared.NATIVE.hass, "config", None), "currency", None
    )
    if household and prefix.upper() == str(household).upper():
        return 1.0
    return None


def _p2p_rate_intervals(
    raw: list, scale: float, lv_feed: bool
) -> list[tuple[datetime, datetime, str, float]]:
    """`{time, value}` rows as intervals. Each row covers only its own
    interval: an explicit `end`, or the LocalVolts feed's 5 minutes. A row
    with neither has no known extent and covers nothing -- never the spacing
    to the next row (Mark Purcell's review of PR #1592)."""
    intervals = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            start = solver_shared.parse_iso(p["time"])
            if "end" in p:
                end = solver_shared.parse_iso(p["end"])
            elif lv_feed:
                end = start + _P2P_DEFAULT_INTERVAL
            else:
                continue
            if end <= start:
                continue
            value = p.get("value")
            if value is None or value == "":
                intervals.append((start, end, P2P_NO_MATCH, 0.0))
                continue
            rate = float(value) * scale
            if math.isnan(rate) or math.isinf(rate):
                continue
            intervals.append((start, end, P2P_RATE, rate))
        except (KeyError, TypeError, ValueError):
            continue
    return intervals


def _p2p_triple_intervals(raw: list) -> list[tuple[datetime, datetime, str, float]]:
    """Raw-triple rows as intervals, `[intervalEnd - intervalDuration,
    intervalEnd)` (5 minutes when undeclared). Matched volume at or below
    0.01 kWh (the pre-#1537 threshold) is an explicit no-match; a row that
    does not state volume or proportion, or states a match without a cost,
    says nothing about its interval."""
    intervals = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            end = solver_shared.parse_iso(
                p["time"] if "time" in p else p["intervalEnd"]
            )
            minutes = p.get("intervalDuration")
            dur = (
                timedelta(minutes=float(minutes)) if minutes else _P2P_DEFAULT_INTERVAL
            )
            vol, prop, cost = (
                p.get(k) for k in ("volume", "proportionP2P", "matchedCost")
            )
            if vol is None or prop is None:
                continue
            matched_vol = float(vol) * float(prop)
            if matched_vol <= 0.01:
                intervals.append((end - dur, end, P2P_NO_MATCH, 0.0))
            elif cost is not None:
                intervals.append((end - dur, end, P2P_RATE, float(cost) / matched_vol))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return intervals


def _p2p_source_observations(
    state: dict | None, sensor_id: str, now: datetime | None
) -> dict:
    """One source as `{status, snapshot, intervals, horizon}`.

    `intervals` is a sorted list of `(start, end, state, value)` the source
    actually published; `horizon` is the `(start, end)` span inside which a
    MISSING interval is an explicit no-match (None: a missing interval is
    uncovered). `status` is P2P_UNAVAILABLE when the source cannot be used at
    all (unreadable, unknown unit, or expired), else "ok"."""
    out: dict = {
        "status": P2P_UNAVAILABLE,
        "snapshot": None,
        "intervals": [],
        "horizon": None,
    }
    if state is None or str(state.get("state", "")).lower() in (
        "unavailable",
        "unknown",
    ):
        return out
    attrs = state["attributes"]
    snapshot = out["snapshot"] = _p2p_snapshot_time(state)
    if (
        now is not None
        and snapshot is not None
        and now - snapshot > P2P_RATE_SOURCE_MAX_AGE
    ):
        solver_shared._LOGGER.debug(
            "Nimbus Solver: P2P matched-rate source %s expired (snapshot %s)",
            sensor_id,
            snapshot.isoformat(),
        )
        return out
    raw = attrs.get("forecast")
    raw = raw if isinstance(raw, list) else []
    horizon = None
    if not _p2p_rows_are_rate_shape(raw):
        intervals = _p2p_triple_intervals(raw)
    else:
        scale = _p2p_unit_scale(attrs.get("unit_of_measurement"))
        if scale is None:
            solver_shared._LOGGER.debug(
                "Nimbus Solver: P2P matched-rate source %s has unit %r, not a "
                "per-kWh price; not used",
                sensor_id,
                attrs.get("unit_of_measurement"),
            )
            return out
        lv_feed = _p2p_is_localvolts_matched_rate_feed(attrs)
        intervals = _p2p_rate_intervals(raw, scale, lv_feed)
        if lv_feed and intervals and snapshot is not None:
            first = min(s for s, _, _, _ in intervals)
            horizon = (min(first, snapshot), max(e for _, e, _, _ in intervals))
    intervals.sort(key=lambda x: x[0])
    out.update(status="ok", intervals=intervals, horizon=horizon)
    return out


def _p2p_observation_at(source: dict, t: datetime) -> tuple[str, float]:
    if source["status"] != "ok":
        return (P2P_UNAVAILABLE, 0.0)
    for start, end, state, value in source["intervals"]:
        if start <= t < end:
            return (state, value)
    horizon = source["horizon"]
    if horizon is not None and horizon[0] <= t < horizon[1]:
        return (P2P_NO_MATCH, 0.0)
    return (P2P_UNCOVERED, 0.0)


def _resolve_p2p_observations(
    sources: list[dict],
    grid_times: list[datetime],
    p2p_window_kw: list[float] | None,
) -> list[float]:
    """Per plan period, the matched rate the sources' observations support.

    Conflict rule: among sources that ANSWER the interval (no-match or a rate,
    zero included), the newest snapshot decides -- an explicit no-match from
    a newer snapshot beats an older match, because two projections of one
    provider are not independent evidence. An unknown snapshot counts as
    oldest; a tie keeps the configured order (primary first).

    Uncovered (no source answers): the #1560 block rule, as before -- inside
    a configured block, the median of the valid matched rates the sources
    did publish (a zero-priced match counts; a no-match does not); without
    blocks, 0. Outside a block, always 0. The blocks and the P2P volume are
    read, never changed: a rate observation creates no commitment."""
    use_blocks = p2p_window_kw is not None and len(p2p_window_kw) == len(grid_times)
    oldest = datetime.min.replace(tzinfo=UTC)
    rank = sorted(
        range(len(sources)),
        key=lambda i: (sources[i]["snapshot"] or oldest, -i),
        reverse=True,
    )
    rates = [
        v
        for s in sources
        if s["status"] == "ok"
        for _, _, st, v in s["intervals"]
        if st == P2P_RATE
    ]
    block_rate = float(statistics.median(rates)) if rates else 0.0
    out = []
    for i, gt in enumerate(grid_times):
        in_window = p2p_window_kw[i] > 0 if use_blocks else True  # NaN is False
        if not in_window:
            out.append(0.0)
            continue
        decided = None
        for idx in rank:
            state, value = _p2p_observation_at(sources[idx], gt)
            if state in (P2P_NO_MATCH, P2P_RATE):
                decided = value if state == P2P_RATE else 0.0
                break
        if decided is not None:
            out.append(float(decided))
        elif use_blocks:
            out.append(block_rate)
        else:
            out.append(0.0)
    return out


def p2p_match_fraction(
    recent_days: int = 5, settlement_sensor: str | None = None
) -> float:
    """Real, empirical fraction of exported energy during the P2P window
    that actually gets matched at the P2P rate (vs reverting to the much
    lower spot rate) -- averaged over the most recent `recent_days` REAL
    SETTLED days from `settlement_sensor` (CONF_SOLVER_P2P_SETTLEMENT_
    HISTORY_SENSOR -- already documented in const.py as retailer-
    agnostic in shape; LocalVolts' own sensor.lv_v2_p2p_confirmed_history,
    the same safe REST-pushed mechanism lv_p2p_daily_recalibrate.py
    already proved out, just happens to be this household's real value
    for it. This function itself was hardcoded to that literal until the
    2026-09-02 audit -- the field already existed and was already wired
    into compute_daily_quality_report(), just never threaded through
    here too).

    Direct, real finding (2026-08-16): assuming 100% match (this
    project's old flat-$0.50 placeholder, PR #308) overstated a real
    day's P2P revenue by ~$10 against LocalVolts' own settled Cashflow
    Breakdown -- confirmed 78% match that specific day. The recent-5-day
    average (not all-time) is used deliberately: the real match rate has
    been genuinely DECLINING (13-day avg 0.686 vs last-5-day avg 0.599,
    computed live 2026-08-16), so the more recent window is the more
    accurate estimate of what's likely to happen tonight, not a stale
    long-run average.

    Falls back to P2P_MATCH_FRACTION_FALLBACK if `settlement_sensor` is
    blank, or the sensor/its history is unavailable for any reason --
    this writer must never crash outright over a secondary accuracy
    refinement.
    """
    if not settlement_sensor:
        return P2P_MATCH_FRACTION_FALLBACK
    try:
        hist = solver_shared.ha_get(settlement_sensor)["attributes"]["history"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return P2P_MATCH_FRACTION_FALLBACK
    dates = sorted(hist.keys())[-recent_days:]
    fracs = []
    for d in dates:
        p2p_kwh = hist[d].get("export_volume", 0.0)
        spot_kwh = hist[d].get("spot_export_volume", 0.0)
        total = p2p_kwh + spot_kwh
        if total > 0:
            fracs.append(p2p_kwh / total)
    if not fracs:
        return P2P_MATCH_FRACTION_FALLBACK
    return sum(fracs) / len(fracs)


def p2p_recent_avg_volume_kwh(
    recent_days: int = 5, settlement_sensor: str | None = None
) -> float:
    """Real, empirical AVERAGE ABSOLUTE kWh of export that gets P2P-matched
    per night -- averaged over the most recent `recent_days` REAL SETTLED
    days, same source (`settlement_sensor`, see p2p_match_fraction()'s
    own docstring for the full field/audit story) and same recency
    reasoning as p2p_match_fraction() above.

    Real, direct fix (2026-08-17, household-confirmed live: "if the
    solver was good it would have kept selling rather than landing
    prematurely"): p2p_match_fraction()'s own FRACTION was being used to
    apply a flat percentage DISCOUNT to every exported kWh's price
    (match_fraction * p2p_rate + (1-match_fraction) * spot_rate,
    uniformly) -- confirmed this systematically understates real P2P
    revenue, since the real mechanism isn't a per-kWh lottery, it's a
    fixed ABSOLUTE nightly volume matched against the household's own
    known historical pattern (documented extensively in this project's
    own CLAUDE.md). This function returns that real absolute volume
    directly, feeding the Solver's own GridConfig.export_bonus_volume_kwh
    (see the nimbus repo's own network.py docstring, "TWO-TIER EXPORT
    BONUS") instead -- the LP now sees the real, UNDILUTED P2P rate for
    up to this many real kWh PER REAL CALENDAR DAY (the constraint resets
    every night, not once across the whole multi-day horizon -- see that
    same docstring for a real bug this exact distinction fixed), falling
    back to spot only beyond that, rather than a diluted average applied
    to everything.

    Falls back to P2P_RECENT_AVG_VOLUME_FALLBACK_KWH if `settlement_sensor`
    is blank, or the sensor/its history is unavailable for any reason --
    this writer must never crash outright over a secondary accuracy
    refinement.
    """
    if not settlement_sensor:
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    try:
        hist = solver_shared.ha_get(settlement_sensor)["attributes"]["history"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    dates = sorted(hist.keys())[-recent_days:]
    volumes = [
        hist[d].get("export_volume", 0.0)
        for d in dates
        if hist[d].get("export_volume", 0.0) > 0
    ]
    if not volumes:
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    return sum(volumes) / len(volumes)
